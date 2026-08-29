from __future__ import annotations

from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class AnomalyDecision:
    severity: str
    drop: float | None
    score: float | None
    baseline: float | None


def classify_traffic(
    current: float | None,
    previous: float | None = None,
    baseline: float | None = None,
    road_closure: bool | None = False,
) -> AnomalyDecision:
    if current is None:
        return AnomalyDecision("UNKNOWN", None, None, baseline)
    if road_closure:
        return AnomalyDecision("UNKNOWN", None, None, baseline)
    reference = previous if previous is not None else baseline
    drop = round(max(0.0, reference - current), 6) if reference is not None else None
    baseline_drop = round(max(0.0, baseline - current), 6) if baseline is not None else 0.0
    if current < settings.detector_strong_threshold:
        severity = "STRONG"
    elif current < settings.detector_moderate_threshold:
        severity = "MODERATE"
    elif current < settings.detector_weak_threshold and max(drop or 0.0, baseline_drop) >= settings.detector_drop_threshold / 2:
        severity = "WEAK"
    else:
        severity = "NORMAL"
    score = round(max(drop or 0.0, baseline_drop, settings.detector_strong_threshold - current), 6)
    return AnomalyDecision(severity, drop, score, baseline)
