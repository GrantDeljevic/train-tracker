from __future__ import annotations

from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class AnomalyDecision:
    severity: str
    drop: float | None
    score: float | None
    baseline: float | None
    min_drop: float | None = None
    abrupt_drop: bool = False
    directional_collapse: bool = False


def classify_traffic(
    current: float | None,
    previous: float | None = None,
    baseline: float | None = None,
    road_closure: bool | None = False,
    *,
    current_min: float | None = None,
    previous_min: float | None = None,
    baseline_min: float | None = None,
    feature_count: int | None = None,
) -> AnomalyDecision:
    if current is None:
        return AnomalyDecision("UNKNOWN", None, None, baseline)
    if road_closure:
        return AnomalyDecision("UNKNOWN", None, None, baseline)
    reference = previous if previous is not None else baseline
    drop = round(max(0.0, reference - current), 6) if reference is not None else None
    baseline_drop = round(max(0.0, baseline - current), 6) if baseline is not None else 0.0
    min_reference = previous_min if previous_min is not None else baseline_min
    min_drop = round(max(0.0, min_reference - current_min), 6) if min_reference is not None and current_min is not None else None
    aggregate_drop = max(drop or 0.0, baseline_drop)
    abrupt_drop = aggregate_drop >= settings.detector_drop_threshold

    # Keep a one-direction collapse useful without allowing a farther,
    # unrelated feature to manufacture an event. The scheduler supplies the
    # minimum of the nearest directional features here.
    directional_collapse = (
        current_min is not None
        and current_min < settings.detector_strong_threshold
        and min_drop is not None
        and min_drop >= settings.detector_drop_threshold / 2
        and (aggregate_drop >= settings.detector_drop_threshold / 2 or (feature_count is not None and feature_count <= 2))
    )
    if current < settings.detector_strong_threshold:
        severity = "STRONG"
    elif current < settings.detector_moderate_threshold:
        severity = "MODERATE"
    elif current < settings.detector_weak_threshold and aggregate_drop >= settings.detector_drop_threshold / 2:
        severity = "WEAK"
    elif abrupt_drop or directional_collapse:
        severity = "WEAK"
    else:
        severity = "NORMAL"
    score = round(max(drop or 0.0, baseline_drop, min_drop or 0.0, settings.detector_strong_threshold - current), 6)
    return AnomalyDecision(severity, drop, score, baseline, min_drop, abrupt_drop, directional_collapse)
