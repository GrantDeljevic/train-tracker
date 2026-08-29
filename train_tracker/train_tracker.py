from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .crossings import TARGET_MILEPOST
from .db import utc_now
from .models import Crossing, CrossingEvent, TrainHypothesis

LOGGER = logging.getLogger(__name__)

MIN_SPEED_MPH = 5.0
MAX_SPEED_MPH = 80.0
EVENT_LOOKBACK = timedelta(hours=6)
HYPOTHESIS_EXPIRY = timedelta(hours=4)


@dataclass(frozen=True)
class InferredSequence:
    event_ids: tuple[int, ...]
    direction: str
    status: str
    evidence_level: str
    source_group: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_crossing_id: int
    last_milepost: float
    estimated_speed: float | None
    eta: datetime | None
    eta_low: datetime | None
    eta_high: datetime | None


TOWARD_ORDERS = {
    "Battle Creek": "ascending",
    "Lansing": "descending",
    "Durand": "descending",
}


def _event_time(event) -> datetime:
    value = event.event_time_estimate
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _sequence_speed(events: Sequence, crossings: dict[int, Crossing]) -> float | None:
    if len(events) < 2:
        return None
    speeds = []
    for first, second in zip(events, events[1:]):
        elapsed_hours = (_event_time(second) - _event_time(first)).total_seconds() / 3600
        distance = abs(crossings[first.crossing_id].milepost - crossings[second.crossing_id].milepost)
        if elapsed_hours <= 0:
            return None
        speed = distance / elapsed_hours
        if not MIN_SPEED_MPH <= speed <= MAX_SPEED_MPH:
            return None
        speeds.append(speed)
    return sum(speeds) / len(speeds)


def _eta_for_sequence(events: Sequence, crossings: dict[int, Crossing], speed: float | None, group: str):
    last_time = _event_time(events[-1])
    if speed is None:
        ranges = {"Battle Creek": (20, 40), "Lansing": (13, 30), "Durand": (45, 90)}
        low, high = ranges[group]
        return last_time + timedelta(minutes=(low + high) / 2), last_time + timedelta(minutes=low), last_time + timedelta(minutes=high)
    distance = abs(crossings[events[-1].crossing_id].milepost - TARGET_MILEPOST)
    lower_speed = max(MIN_SPEED_MPH, speed * 0.65)
    upper_speed = min(MAX_SPEED_MPH, speed * 1.35)
    low_bound = _event_time(events[-1])
    high_bound = low_bound
    if events[-1].event_time_low:
        low_bound = _event_time(type("T", (), {"event_time_estimate": events[-1].event_time_low})())
    if events[-1].event_time_high:
        high_bound = _event_time(type("T", (), {"event_time_estimate": events[-1].event_time_high})())
    eta = last_time + timedelta(hours=distance / speed) if speed else None
    eta_low = low_bound + timedelta(hours=distance / upper_speed)
    eta_high = high_bound + timedelta(hours=distance / lower_speed)
    return eta, eta_low, eta_high


def _find_next(events: Sequence, current_index: int, order: list[int], crossings: dict[int, Crossing], direction: str):
    current = events[current_index]
    current_pos = order.index(current.crossing_id)
    valid = []
    for candidate_index in range(current_index + 1, len(events)):
        candidate = events[candidate_index]
        if candidate.crossing_id not in order:
            continue
        candidate_pos = order.index(candidate.crossing_id)
        if candidate_pos <= current_pos:
            continue
        elapsed = (_event_time(candidate) - _event_time(current)).total_seconds() / 3600
        if elapsed <= 0:
            continue
        distance = abs(crossings[current.crossing_id].milepost - crossings[candidate.crossing_id].milepost)
        speed = distance / elapsed
        if MIN_SPEED_MPH <= speed <= MAX_SPEED_MPH:
            valid.append((candidate_index, speed))
    return min(valid, key=lambda item: item[0]) if valid else None


def infer_group_sequences(events: Iterable, crossings: dict[int, Crossing], group_name: str) -> list[InferredSequence]:
    relevant = sorted(
        [event for event in events if event.crossing_id in crossings and crossings[event.crossing_id].group_name == group_name],
        key=_event_time,
    )
    if not relevant:
        return []
    order_ids = sorted(
        {event.crossing_id for event in relevant},
        key=lambda crossing_id: crossings[crossing_id].milepost,
        reverse=TOWARD_ORDERS[group_name] == "descending",
    )
    reverse_ids = list(reversed(order_ids))
    results: list[InferredSequence] = []
    used: set[tuple[int, ...]] = set()
    for direction, order in (("toward", order_ids), ("away", reverse_ids)):
        for start_index, start in enumerate(relevant):
            if start.crossing_id not in order:
                continue
            sequence = [start]
            current_index = start_index
            while len(sequence) < len(order):
                match = _find_next(relevant, current_index, order, crossings, direction)
                if match is None:
                    break
                current_index = match[0]
                sequence.append(relevant[current_index])
            fingerprint = tuple(event.id for event in sequence)
            if fingerprint in used:
                continue
            used.add(fingerprint)
            speed = _sequence_speed(sequence, crossings)
            toward = direction == "toward"
            if len(sequence) == 1:
                status = "POSSIBLE"
                evidence = "POSSIBLE"
                direction_name = "unknown"
            elif toward:
                status = "HIGH_CONFIDENCE" if len(sequence) >= 3 else "APPROACHING"
                evidence = status
                direction_name = {"Battle Creek": "from_battle_creek", "Lansing": "from_lansing", "Durand": "from_durand"}[group_name]
            else:
                status = "PASSED/EXPIRED"
                evidence = "NONE"
                direction_name = "away_from_charlotte"
            eta, eta_low, eta_high = _eta_for_sequence(sequence, crossings, speed if len(sequence) >= 2 else None, group_name)
            results.append(InferredSequence(fingerprint, direction_name, status, evidence, group_name, _event_time(sequence[0]), _event_time(sequence[-1]), sequence[-1].crossing_id, crossings[sequence[-1].crossing_id].milepost, speed, eta, eta_low, eta_high))
    # The single-event result is useful, but paired results supersede it for the same event.
    paired_ids = {event_id for result in results if len(result.event_ids) > 1 for event_id in result.event_ids}
    return [result for result in results if len(result.event_ids) > 1 or result.event_ids[0] not in paired_ids]


def _durand_lansing_association(results: list[InferredSequence], events_by_id: dict[int, CrossingEvent], crossings: dict[int, Crossing]) -> list[InferredSequence]:
    durand = [result for result in results if result.source_group == "Durand" and result.direction == "from_durand" and len(result.event_ids) >= 2]
    lansing = [result for result in results if result.source_group == "Lansing" and result.direction == "from_lansing" and len(result.event_ids) >= 2]
    associations = []
    for west in durand:
        for east in lansing:
            first_lansing = events_by_id[east.event_ids[0]]
            elapsed = (_event_time(first_lansing) - west.last_seen_at).total_seconds() / 3600
            distance = abs(west.last_milepost - crossings[first_lansing.crossing_id].milepost)
            if elapsed <= 0 or not MIN_SPEED_MPH <= distance / elapsed <= MAX_SPEED_MPH:
                continue
            combined = tuple(dict.fromkeys(west.event_ids + east.event_ids))
            associations.append(InferredSequence(combined, "from_lansing", "HIGH_CONFIDENCE", "HIGH_CONFIDENCE", "Durand -> Lansing", west.first_seen_at, east.last_seen_at, east.last_crossing_id, east.last_milepost, east.estimated_speed, east.eta, east.eta_low, east.eta_high))
    return associations


def refresh_hypotheses(session: Session, now: datetime | None = None) -> list[TrainHypothesis]:
    now = now or utc_now()
    cutoff = now - EVENT_LOOKBACK
    crossings = {crossing.id: crossing for crossing in session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all()}
    events = list(session.scalars(select(CrossingEvent).where(CrossingEvent.event_time_estimate >= cutoff).order_by(CrossingEvent.event_time_estimate)).all())
    events_by_id = {event.id: event for event in events}
    results: list[InferredSequence] = []
    for group in ("Battle Creek", "Lansing", "Durand"):
        results.extend(infer_group_sequences(events, crossings, group))
    results.extend(_durand_lansing_association(results, events_by_id, crossings))
    existing = {tuple(row.event_ids): row for row in session.scalars(select(TrainHypothesis)).all() if row.event_ids}
    active_rows = []
    for result in results:
        row = existing.get(result.event_ids)
        if row is None:
            row = TrainHypothesis(event_ids=list(result.event_ids), created_at=now)
            session.add(row)
        row.direction = result.direction
        row.status = result.status
        row.first_seen_at = result.first_seen_at
        row.last_seen_at = result.last_seen_at
        row.last_crossing_id = result.last_crossing_id
        row.last_milepost = result.last_milepost
        row.estimated_speed = result.estimated_speed
        row.eta = result.eta
        row.eta_low = result.eta_low
        row.eta_high = result.eta_high
        row.evidence_level = result.evidence_level
        row.source_group = result.source_group
        row.updated_at = now
        active_rows.append(row)
    for row in session.scalars(select(TrainHypothesis)).all():
        if row not in active_rows and row.status not in {"PASSED/EXPIRED"} and row.last_seen_at < now - HYPOTHESIS_EXPIRY:
            row.status = "PASSED/EXPIRED"
            row.updated_at = now
    session.flush()
    return active_rows

