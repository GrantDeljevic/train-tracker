from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select

from .config import settings
from .calibration import update_crossing_quality
from .db import SessionLocal, session_scope, utc_now
from .detection import classify_traffic
from .models import Crossing, CrossingEvent, SystemState, TrafficObservation
from .sheets import GoogleSheetsArchive
from .tomtom import RequestBudgetExceeded, TileKey, TomTomError
from .traffic import TileMapping, observation_from_tiles
from .train_tracker import refresh_hypotheses
from .usage import UsageService, usage_month

LOGGER = logging.getLogger(__name__)

GROUP_OFFSETS = {"Battle Creek": 0, "Lansing": 20, "Durand": 40}
SCHEDULE_STATE_VERSION = 2


def interval_for(crossing: Crossing, burst: bool = False, projected: int | None = None) -> int:
    if burst:
        return 60
    if crossing.role == "primary":
        return 120
    if projected is not None and projected >= settings.soft_request_budget:
        return 360 if projected >= settings.monthly_request_budget else 300
    return 240


def phase_anchor(crossing: Crossing, now: datetime, burst: bool = False, projected: int | None = None) -> datetime:
    """Return the most recent scheduled slot for a crossing.

    Polls may happen a few seconds after a slot because the process or the
    invoking scheduler has jitter. Keeping this slot, rather than the actual
    catch-up time, as the cadence anchor prevents a bootstrap poll from
    synchronizing otherwise staggered crossings.
    """
    interval = interval_for(crossing, burst, projected)
    phase = (GROUP_OFFSETS.get(crossing.group_name, 0) + (crossing.phase_sec or 0)) % interval
    epoch = now.timestamp()
    anchor = epoch - ((epoch - phase) % interval)
    return datetime.fromtimestamp(anchor, timezone.utc)


def due_crossings(crossings, now: datetime, last_polled: dict[int, datetime], burst_groups: set[str] | None = None, projected: int | None = None):
    burst_groups = burst_groups or set()
    epoch = now.timestamp()
    due = []
    for crossing in crossings:
        if not crossing.enabled:
            continue
        burst = crossing.group_name in burst_groups
        interval = interval_for(crossing, burst, projected)
        last = last_polled.get(crossing.id)
        if last is not None:
            if (now - last).total_seconds() >= interval:
                due.append(crossing)
            continue
        phase = GROUP_OFFSETS.get(crossing.group_name, 0) + crossing.phase_sec
        aligned = epoch - (epoch % interval) + phase
        if aligned > epoch:
            aligned -= interval
        if epoch - aligned < 15:
            due.append(crossing)
    return due


class PollScheduler:
    def __init__(self, tomtom_client, session_factory=SessionLocal, usage_service: UsageService | None = None, archive: GoogleSheetsArchive | None = None, initial_poll_all: bool = False):
        self.tomtom = tomtom_client
        self.session_factory = session_factory
        self.usage = usage_service or UsageService(session_factory)
        self.archive = archive
        self.initial_poll_all = initial_poll_all
        self.last_polled: dict[int, datetime] = {}
        self.burst_until: dict[str, datetime] = {}
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.stop_event = asyncio.Event()

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def restore_runtime_state(self, state: dict | None) -> None:
        """Restore the small state needed when a scheduled instance is cold-started."""
        if not state:
            return
        with self.session_factory() as session:
            crossings = {crossing.fra_id: crossing for crossing in session.scalars(select(Crossing)).all()}
            for fra_id, value in (state.get("last_polled") or {}).items():
                crossing = crossings.get(fra_id)
                parsed = self._parse_time(value)
                if crossing and parsed:
                    self.last_polled[crossing.id] = parsed
            self.burst_until = {
                str(group): parsed
                for group, value in (state.get("burst_until") or {}).items()
                if (parsed := self._parse_time(value)) is not None
            }
            if int(state.get("version") or 1) < SCHEDULE_STATE_VERSION:
                # Version 1 bootstrapped every crossing at one timestamp in
                # serverless mode. Re-anchor that legacy snapshot to the
                # configured phases once, without changing observations.
                burst_groups = self._burst_groups(utc_now())
                self.last_polled = {
                    crossing_id: phase_anchor(
                        crossing,
                        moment,
                        burst=crossing.group_name in burst_groups,
                    )
                    for crossing_id, moment in self.last_polled.items()
                    if (crossing := next((item for item in crossings.values() if item.id == crossing_id), None)) is not None
                }
            self.last_run = self._parse_time(state.get("last_run"))
            self.last_error = state.get("last_error") or None
            restored_usage = state.get("usage")
            if isinstance(restored_usage, dict) and restored_usage.get("month") == usage_month():
                self.usage.restore(usage_month(), restored_usage)

            for item in state.get("observations") or []:
                crossing = crossings.get(item.get("fra_id"))
                observed_at = self._parse_time(item.get("observed_at"))
                if not crossing or not observed_at:
                    continue
                exists = session.scalar(
                    select(TrafficObservation).where(
                        TrafficObservation.crossing_id == crossing.id,
                        TrafficObservation.observed_at == observed_at,
                    ).limit(1)
                )
                if exists:
                    continue
                session.add(TrafficObservation(
                    crossing_id=crossing.id,
                    observed_at=observed_at,
                    tile_fetched_at=self._parse_time(item.get("tile_fetched_at")),
                    traffic_level_min=item.get("traffic_level_min"),
                    traffic_level_median=item.get("traffic_level_median"),
                    directional_values=item.get("directional_values"),
                    road_coverage=item.get("road_coverage"),
                    road_closure=item.get("road_closure"),
                    feature_count=int(item.get("feature_count") or 0),
                    usable=bool(item.get("usable")),
                    severity=item.get("severity") or "UNKNOWN",
                    anomaly_drop=item.get("anomaly_drop"),
                    anomaly_score=item.get("anomaly_score"),
                    status=item.get("status"),
                    error_detail=item.get("error_detail"),
                    tile_key=item.get("tile_key"),
                ))

            for item in state.get("events") or []:
                crossing = crossings.get(item.get("fra_id"))
                event_time = self._parse_time(item.get("event_time_estimate"))
                if not crossing or not event_time:
                    continue
                event_id = item.get("id")
                exists = session.get(CrossingEvent, int(event_id)) if event_id not in (None, "") else None
                if exists:
                    continue
                session.add(CrossingEvent(
                    id=int(event_id) if event_id not in (None, "") else None,
                    crossing_id=crossing.id,
                    event_time_estimate=event_time,
                    event_time_low=self._parse_time(item.get("event_time_low")),
                    event_time_high=self._parse_time(item.get("event_time_high")),
                    severity=item.get("severity") or "UNKNOWN",
                    evidence_json=item.get("evidence_json") or {},
                    created_at=self._parse_time(item.get("created_at")) or event_time,
                ))
            session.commit()
        self.initial_poll_all = False

    def _runtime_state(self, now: datetime) -> dict:
        with self.session_factory() as session:
            crossings = {crossing.id: crossing for crossing in session.scalars(select(Crossing)).all()}
            observations = list(session.scalars(select(TrafficObservation).order_by(TrafficObservation.observed_at.desc()).limit(200)).all())
            per_crossing: dict[int, int] = {}
            recent_observations = []
            for item in observations:
                per_crossing[item.crossing_id] = per_crossing.get(item.crossing_id, 0)
                if per_crossing[item.crossing_id] >= 20:
                    continue
                per_crossing[item.crossing_id] += 1
                crossing = crossings.get(item.crossing_id)
                if not crossing:
                    continue
                recent_observations.append({
                    "fra_id": crossing.fra_id,
                    "observed_at": item.observed_at.isoformat(),
                    "tile_fetched_at": item.tile_fetched_at.isoformat() if item.tile_fetched_at else None,
                    "traffic_level_min": item.traffic_level_min,
                    "traffic_level_median": item.traffic_level_median,
                    "directional_values": item.directional_values,
                    "road_coverage": item.road_coverage,
                    "road_closure": item.road_closure,
                    "feature_count": item.feature_count,
                    "usable": item.usable,
                    "severity": item.severity,
                    "anomaly_drop": item.anomaly_drop,
                    "anomaly_score": item.anomaly_score,
                    "status": item.status,
                    "error_detail": item.error_detail,
                    "tile_key": item.tile_key,
                })
            cutoff = now - timedelta(hours=6)
            events = session.scalars(select(CrossingEvent).where(CrossingEvent.event_time_estimate >= cutoff).order_by(CrossingEvent.event_time_estimate)).all()
            recent_events = []
            for item in events:
                crossing = crossings.get(item.crossing_id)
                if not crossing:
                    continue
                recent_events.append({
                    "id": item.id,
                    "fra_id": crossing.fra_id,
                    "event_time_estimate": item.event_time_estimate.isoformat(),
                    "event_time_low": item.event_time_low.isoformat() if item.event_time_low else None,
                    "event_time_high": item.event_time_high.isoformat() if item.event_time_high else None,
                    "severity": item.severity,
                    "evidence_json": item.evidence_json,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                })
        return {
            "version": SCHEDULE_STATE_VERSION,
            "last_polled": {crossings[crossing_id].fra_id: moment.isoformat() for crossing_id, moment in self.last_polled.items() if crossing_id in crossings},
            "burst_until": {group: moment.isoformat() for group, moment in self.burst_until.items() if moment > now},
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_error": self.last_error,
            "usage": self.usage.snapshot(),
            "observations": recent_observations,
            "events": recent_events,
        }

    def _save_runtime_state(self, now: datetime) -> None:
        if self.archive is None or not self.archive.connected:
            return
        try:
            self.archive.save_runtime_state(self._runtime_state(now), recorded_at=now)
        except Exception as exc:
            LOGGER.exception("Unable to persist runtime state snapshot: %s", exc)

    def _burst_groups(self, now: datetime) -> set[str]:
        return {group for group, until in self.burst_until.items() if until > now}

    def _mark_burst(self, group: str, now: datetime) -> None:
        self.burst_until[group] = max(self.burst_until.get(group, now), now + timedelta(minutes=20))

    def _save_system_state(self, key: str, value: dict) -> None:
        with session_scope() as session:
            row = session.get(SystemState, key)
            if row is None:
                row = SystemState(key=key, value_json=value, updated_at=utc_now())
                session.add(row)
            else:
                row.value_json = value
                row.updated_at = utc_now()

    def _previous_metrics(self, session, crossing_id: int, now: datetime):
        observations = list(
            session.scalars(
                select(TrafficObservation)
                .where(TrafficObservation.crossing_id == crossing_id, TrafficObservation.observed_at < now, TrafficObservation.usable.is_(True))
                .order_by(TrafficObservation.observed_at.desc())
                .limit(20)
            ).all()
        )
        previous = observations[0].traffic_level_median if observations else None
        levels = [item.traffic_level_median for item in observations if item.traffic_level_median is not None]
        return previous, median(levels) if levels else None

    def _create_event_if_new(self, session, crossing: Crossing, observation: TrafficObservation, decision) -> CrossingEvent | None:
        if decision.severity not in {"WEAK", "MODERATE", "STRONG"}:
            return None
        last_event = session.scalar(
            select(CrossingEvent)
            .where(CrossingEvent.crossing_id == crossing.id)
            .order_by(CrossingEvent.event_time_estimate.desc())
            .limit(1)
        )
        if last_event is not None:
            normal_after = session.scalar(
                select(TrafficObservation)
                .where(
                    TrafficObservation.crossing_id == crossing.id,
                    TrafficObservation.observed_at > last_event.event_time_high,
                    TrafficObservation.severity == "NORMAL",
                )
                .order_by(TrafficObservation.observed_at.desc())
                .limit(1)
            )
            if normal_after is None and (observation.observed_at - last_event.event_time_estimate).total_seconds() < 20 * 60:
                return None
        previous = session.scalar(
            select(TrafficObservation)
            .where(TrafficObservation.crossing_id == crossing.id, TrafficObservation.observed_at < observation.observed_at)
            .order_by(TrafficObservation.observed_at.desc())
            .limit(1)
        )
        low = previous.observed_at if previous else observation.observed_at - timedelta(seconds=crossing.poll_interval_sec or 240)
        high = observation.observed_at
        event_time = low + (high - low) / 2
        event = CrossingEvent(
            crossing_id=crossing.id,
            event_time_estimate=event_time,
            event_time_low=low,
            event_time_high=high,
            severity=decision.severity,
            evidence_json={
                "traffic_level_min": observation.traffic_level_min,
                "traffic_level_median": observation.traffic_level_median,
                "previous_level": previous.traffic_level_median if previous else None,
                "baseline_level": decision.baseline,
                "drop": decision.drop,
                "score": decision.score,
                "feature_count": observation.feature_count,
                "status": observation.status,
            },
            created_at=utc_now(),
        )
        session.add(event)
        return event

    @staticmethod
    def _observation_payload(crossing: Crossing, observation: TrafficObservation, recorded_at: datetime) -> dict:
        return {
            "recorded_at": recorded_at,
            "crossing_fra_id": crossing.fra_id,
            "crossing_name": crossing.name,
            "group": crossing.group_name,
            "milepost": crossing.milepost,
            "observed_at": observation.observed_at,
            "tile_fetched_at": observation.tile_fetched_at,
            "traffic_level_min": observation.traffic_level_min,
            "traffic_level_median": observation.traffic_level_median,
            "directional_values": observation.directional_values,
            "road_coverage": observation.road_coverage,
            "road_closure": observation.road_closure,
            "feature_count": observation.feature_count,
            "usable": observation.usable,
            "severity": observation.severity,
            "anomaly_drop": observation.anomaly_drop,
            "anomaly_score": observation.anomaly_score,
            "status": observation.status,
            "error_detail": observation.error_detail,
            "tile_key": observation.tile_key,
        }

    @staticmethod
    def _event_payload(crossing: Crossing, event: CrossingEvent, recorded_at: datetime) -> dict:
        return {
            "recorded_at": recorded_at,
            "event_id": event.id,
            "crossing_fra_id": crossing.fra_id,
            "crossing_name": crossing.name,
            "group": crossing.group_name,
            "milepost": crossing.milepost,
            "event_time_estimate": event.event_time_estimate,
            "event_time_low": event.event_time_low,
            "event_time_high": event.event_time_high,
            "severity": event.severity,
            "evidence_json": event.evidence_json,
        }

    @staticmethod
    def _hypothesis_payload(row, crossing_by_id: dict[int, Crossing], recorded_at: datetime) -> dict:
        crossing = crossing_by_id.get(row.last_crossing_id) if row.last_crossing_id else None
        return {
            "recorded_at": recorded_at,
            "hypothesis_id": row.id,
            "direction": row.direction,
            "status": row.status,
            "evidence_level": row.evidence_level,
            "source_group": row.source_group,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "last_crossing_fra_id": crossing.fra_id if crossing else "",
            "last_milepost": row.last_milepost,
            "estimated_speed_mph": row.estimated_speed,
            "eta": row.eta,
            "eta_low": row.eta_low,
            "eta_high": row.eta_high,
            "event_ids": row.event_ids,
        }

    def poll_crossing(self, crossing_id: int, now: datetime | None = None) -> bool:
        now = now or utc_now()
        sheet_observation = None
        sheet_event = None
        sheet_hypotheses = []
        sheet_calibration = None
        with session_scope() as session:
            crossing = session.get(Crossing, crossing_id)
            if crossing is None or not crossing.enabled:
                return False
            mapping = TileMapping.from_dict(crossing.tile_mapping_json)
            tile_key = mapping.tiles if mapping else ()
            try:
                responses = [self.tomtom.fetch_tile(key) for key in tile_key]
                result = observation_from_tiles(crossing, responses, now)
                previous, baseline = self._previous_metrics(session, crossing.id, now)
                decision = classify_traffic(result.traffic_level_median, previous, baseline, result.road_closure)
                observation = TrafficObservation(
                    crossing_id=crossing.id,
                    observed_at=result.observed_at,
                    tile_fetched_at=result.tile_fetched_at,
                    traffic_level_min=result.traffic_level_min,
                    traffic_level_median=result.traffic_level_median,
                    directional_values=result.directional_values,
                    road_coverage=result.road_coverage,
                    road_closure=result.road_closure,
                    feature_count=result.feature_count,
                    usable=result.usable,
                    severity=decision.severity,
                    anomaly_drop=decision.drop,
                    anomaly_score=decision.score,
                    status=result.status,
                    error_detail=result.error_detail,
                    tile_key=",".join(key.as_string() for key in tile_key),
                )
                session.add(observation)
                created = self._create_event_if_new(session, crossing, observation, decision)
                session.flush()
                quality = update_crossing_quality(session, crossing, now)
                if created is not None:
                    self._mark_burst(crossing.group_name, now)
                hypotheses = refresh_hypotheses(session, now)
                sheet_observation = self._observation_payload(crossing, observation, now)
                sheet_event = self._event_payload(crossing, created, now) if created is not None else None
                crossing_by_id = {item.id: item for item in session.scalars(select(Crossing)).all()}
                sheet_hypotheses = [self._hypothesis_payload(row, crossing_by_id, now) for row in hypotheses]
                if quality.get("observation_count", 0) == 1 or quality.get("observation_count", 0) % 10 == 0:
                    sheet_calibration = {
                        "recorded_at": now,
                        "crossing_fra_id": crossing.fra_id,
                        "crossing_name": crossing.name,
                        "group": crossing.group_name,
                        **quality,
                    }
                usable = result.usable
            except (TomTomError, RequestBudgetExceeded, ValueError) as exc:
                observation = TrafficObservation(
                    crossing_id=crossing.id,
                    observed_at=now,
                    usable=False,
                    severity="UNKNOWN",
                    status="ERROR",
                    error_detail=str(exc)[:1000],
                    tile_key=",".join(key.as_string() for key in tile_key),
                )
                session.add(observation)
                self.last_error = str(exc)
                sheet_observation = self._observation_payload(crossing, observation, now)
                usable = False
        if self.archive is not None:
            if sheet_observation:
                self.archive.enqueue_observation(sheet_observation)
            if sheet_event:
                self.archive.enqueue_event(sheet_event)
            for payload in sheet_hypotheses:
                self.archive.enqueue_hypothesis(payload)
            if sheet_calibration:
                self.archive.enqueue_calibration(sheet_calibration)
        return usable

    def poll_due(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        with self.session_factory() as session:
            crossings = list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())
            projected = self.usage.projected_monthly_requests(session)
        burst_groups = self._burst_groups(now)
        bootstrap = self.initial_poll_all and not self.last_polled and self.last_run is None
        if bootstrap:
            due = crossings
        else:
            due = due_crossings(crossings, now, self.last_polled, burst_groups, projected)
        count = 0
        cycle_failed = False
        for crossing in due:
            if not self.poll_crossing(crossing.id, now):
                cycle_failed = True
            # Keep the configured phase as the cadence anchor even when the
            # provider or invoking scheduler is a few seconds late. This
            # also repairs synchronized legacy snapshots on their next poll.
            self.last_polled[crossing.id] = phase_anchor(
                crossing,
                now,
                burst=crossing.group_name in burst_groups,
                projected=projected,
            )
            count += 1
        if due and not cycle_failed:
            self.last_error = None
        self.last_run = now
        try:
            self._save_system_state("poller", {"last_run": now.isoformat(), "last_error": self.last_error, "polled": count})
        except Exception as exc:
            LOGGER.exception("Unable to persist poller health: %s", exc)
        self._save_runtime_state(now)
        return count

    def flush_archive_if_due(self, now: datetime | None = None, force: bool = False) -> bool:
        if self.archive is None or not self.archive.connected:
            return False
        snapshot = self.usage.snapshot()
        snapshot["recorded_at"] = now or utc_now()
        self.archive.enqueue_usage(snapshot)
        return self.archive.flush(force=force)

    async def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.poll_due)
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.exception("Poll cycle failed")
            try:
                await asyncio.to_thread(self.flush_archive_if_due)
            except Exception as exc:
                LOGGER.exception("Sheet archive cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self.stop_event.set()
