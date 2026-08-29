from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select

from .models import Crossing, CrossingEvent, TrainHypothesis, TrafficObservation


def calculate_crossing_quality(session, crossing_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    observations = list(session.scalars(select(TrafficObservation).where(TrafficObservation.crossing_id == crossing_id, TrafficObservation.observed_at >= cutoff)).all())
    usable = [item for item in observations if item.usable and item.traffic_level_median is not None]
    anomalies = [item for item in observations if item.severity in {"WEAK", "MODERATE", "STRONG"}]
    events = list(session.scalars(select(CrossingEvent).where(CrossingEvent.crossing_id == crossing_id, CrossingEvent.event_time_estimate >= cutoff)).all())
    event_ids = {event.id for event in events}
    participating = 0
    for hypothesis in session.scalars(select(TrainHypothesis).where(TrainHypothesis.updated_at >= cutoff)).all():
        hypothesis_event_ids = set(hypothesis.event_ids or [])
        if hypothesis_event_ids & event_ids and hypothesis.status == "HIGH_CONFIDENCE":
            participating += 1
    by_hour: dict[str, dict[str, int]] = defaultdict(lambda: {"observations": 0, "usable": 0})
    for item in observations:
        hour = str((item.observed_at.hour if item.observed_at.tzinfo else item.observed_at.replace(tzinfo=timezone.utc).hour)).zfill(2)
        by_hour[hour]["observations"] += 1
        if item.usable:
            by_hour[hour]["usable"] += 1
    hourly_usefulness = {hour: round(value["usable"] / value["observations"], 3) if value["observations"] else None for hour, value in by_hour.items()}
    return {
        "window_days": 30,
        "observation_count": len(observations),
        "valid_flow_percentage": round(len(usable) / len(observations), 3) if observations else None,
        "anomaly_frequency": round(len(anomalies) / len(observations), 3) if observations else None,
        "isolated_anomaly_rate": round(max(0, len(anomalies) - len(events)) / len(anomalies), 3) if anomalies else None,
        "typical_baseline": round(median([item.traffic_level_median for item in usable]), 4) if usable else None,
        "direction_confirmed_sequences": participating,
        "hourly_usefulness": hourly_usefulness,
        "updated_at": now.isoformat(),
    }


def update_crossing_quality(session, crossing: Crossing, now: datetime | None = None) -> dict:
    quality = calculate_crossing_quality(session, crossing.id, now)
    mapping = dict(crossing.tile_mapping_json or {})
    geometry_score = mapping.get("geometry_score", crossing.coverage_score)
    valid_percentage = quality.get("valid_flow_percentage")
    if geometry_score is not None and valid_percentage is not None and quality["observation_count"] >= 10:
        crossing.coverage_score = round(0.4 * float(geometry_score) + 0.6 * float(valid_percentage), 4)
    mapping["quality"] = quality
    crossing.tile_mapping_json = mapping
    return quality

