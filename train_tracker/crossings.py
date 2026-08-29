from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BASE_DIR
from .db import utc_now
from .fra import FRAClient, FRARecord
from .models import Crossing, SystemState

LOGGER = logging.getLogger(__name__)

TARGET_MILEPOST = 201.890
TARGET_FRA_ID = "283602W"
TARGET_NAME = "Lawrence St / M-79"
STATIC_CONFIG_PATH = BASE_DIR / "config" / "validated_crossings.json"


class StaticConfigurationError(RuntimeError):
    """Raised when the locally validated crossing configuration is unusable."""


@dataclass(frozen=True)
class CrossingSpec:
    fra_id: str
    name: str
    group_name: str
    expected_milepost: float | None = None
    candidate: bool = False


FIXED_SPECS: tuple[CrossingSpec, ...] = (
    CrossingSpec("283556X", "McAllister Rd", "Battle Creek", 181.160),
    CrossingSpec("283559T", "Pine Lake Rd", "Battle Creek", 182.480),
    CrossingSpec("283632N", "Creyts Rd", "Lansing", 215.410),
    CrossingSpec("283634C", "Millett Hwy", "Lansing", 216.190),
    CrossingSpec("283716J", "Oak Street", "Durand", 253.030),
    CrossingSpec("283714V", "Pittsburg Rd", "Durand", 251.730),
    CrossingSpec("283706D", "Grand River Rd", "Durand", 248.650),
)
CANDIDATE_SPECS: tuple[CrossingSpec, ...] = (
    CrossingSpec("283562B", "T Drive/Burrows Rd", "Battle Creek", 184.630, True),
    CrossingSpec("283563H", "Mulvaney Rd", "Battle Creek", 185.210, True),
)


def _aadt_score(record: FRARecord) -> float:
    # AADT is a useful prior, but never outranks an unavailable flow signal.
    return min(max((record.aadt or 0) / 10_000, 0.0), 1.0)


def choose_battle_creek_candidate(
    records: Iterable[FRARecord], coverage_scores: dict[str, float | None] | None = None
) -> FRARecord:
    coverage_scores = coverage_scores or {}
    records = list(records)
    if not records:
        raise ValueError("no Battle Creek candidate records")
    def score(record: FRARecord) -> tuple[float, float, float]:
        coverage = coverage_scores.get(record.fra_id)
        # Live coverage is the first tie-breaker; AADT is the primary prior.
        live = coverage if coverage is not None else -1.0
        return (live, _aadt_score(record), record.aadt or 0)
    return max(records, key=score)


def _role_score(crossing: Crossing) -> tuple[float, float, int]:
    return (1.0 if crossing.coverage_score is not None and crossing.coverage_score >= 0.50 else 0.0,
            min((crossing.aadt or 0) / 10_000, 1.0), crossing.aadt or 0)


class CrossingManager:
    def __init__(self, fra_client: FRAClient):
        self.fra_client = fra_client

    def resolve_all(self) -> dict[str, FRARecord]:
        records = {spec.fra_id: self.fra_client.resolve_crossing(spec.fra_id) for spec in FIXED_SPECS}
        candidates = [self.fra_client.resolve_crossing(spec.fra_id) for spec in CANDIDATE_SPECS]
        records[choose_battle_creek_candidate(candidates).fra_id] = choose_battle_creek_candidate(candidates)
        return records

    def sync(
        self,
        session: Session,
        coverage_scores: dict[str, float | None] | None = None,
        tile_configs: dict[str, dict] | None = None,
        selected_record: FRARecord | None = None,
        replacement_specs: dict[str, CrossingSpec] | None = None,
        replacement_records: dict[str, FRARecord] | None = None,
        exclude_fra_ids: set[str] | None = None,
    ) -> list[Crossing]:
        coverage_scores = coverage_scores or {}
        tile_configs = tile_configs or {}
        records = {spec.fra_id: self.fra_client.resolve_crossing(spec.fra_id) for spec in FIXED_SPECS}
        candidate_records = [self.fra_client.resolve_crossing(spec.fra_id) for spec in CANDIDATE_SPECS]
        chosen = selected_record or choose_battle_creek_candidate(candidate_records, coverage_scores)
        records[chosen.fra_id] = chosen
        for fra_id in exclude_fra_ids or set():
            records.pop(fra_id, None)
        records.update(replacement_records or {})
        specs = {spec.fra_id: spec for spec in FIXED_SPECS + CANDIDATE_SPECS}
        specs.update(replacement_specs or {})
        for record in records.values():
            specs.setdefault(record.fra_id, CrossingSpec(record.fra_id, record.street.title(), "Battle Creek" if record.milepost < TARGET_MILEPOST else "Lansing", record.milepost))
        now = utc_now()
        for fra_id, record in records.items():
            spec = specs[fra_id]
            crossing = session.scalar(select(Crossing).where(Crossing.fra_id == fra_id))
            if crossing is None:
                crossing = Crossing(fra_id=fra_id, created_at=now)
                session.add(crossing)
            crossing.name = spec.name
            crossing.group_name = spec.group_name
            crossing.milepost = record.milepost
            crossing.latitude = record.latitude
            crossing.longitude = record.longitude
            crossing.railroad = record.railroad
            crossing.subdivision = record.subdivision
            crossing.aadt = record.aadt
            crossing.aadt_year = record.aadt_year
            crossing.fra_revision_date = record.revision_date
            crossing.last_fra_sync_at = now
            crossing.coverage_score = coverage_scores.get(fra_id, crossing.coverage_score)
            crossing.tile_mapping_json = tile_configs.get(fra_id, crossing.tile_mapping_json)
            if crossing.tile_mapping_json and crossing.tile_mapping_json.get("zoom"):
                crossing.tile_zoom = crossing.tile_mapping_json["zoom"]
            crossing.enabled = True
            crossing.updated_at = now
        selected_ids = set(records)
        for crossing in session.scalars(select(Crossing)).all():
            if crossing.fra_id not in selected_ids and crossing.group_name in {"Battle Creek", "Lansing", "Durand"}:
                crossing.enabled = False
                crossing.updated_at = now
        session.flush()
        self.assign_roles(session)
        return list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())

    @staticmethod
    def assign_roles(session: Session) -> None:
        now = utc_now()
        for group in {"Battle Creek", "Lansing", "Durand"}:
            crossings = list(session.scalars(select(Crossing).where(Crossing.group_name == group, Crossing.enabled.is_(True))).all())
            if not crossings:
                continue
            primary = max(crossings, key=_role_score)
            backups = sorted((item for item in crossings if item.id != primary.id), key=lambda item: item.milepost)
            for crossing in crossings:
                crossing.role = "primary" if crossing.id == primary.id else "backup"
                crossing.poll_interval_sec = 120 if crossing.id == primary.id else 240
                # Primary polls on the two-minute boundary; backups land at
                # 1 and 3 minutes so a three-crossing group stays staggered.
                crossing.phase_sec = 0 if crossing.id == primary.id else 60 + backups.index(crossing) * 120
                crossing.updated_at = now


def read_static_configuration(path: str | Path = STATIC_CONFIG_PATH) -> dict:
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StaticConfigurationError(f"validated crossing config is missing: {config_path}") from error
    except json.JSONDecodeError as error:
        raise StaticConfigurationError(f"validated crossing config is not valid JSON: {config_path}") from error
    if payload.get("schema_version") != 1:
        raise StaticConfigurationError("validated crossing config has an unsupported schema_version")
    target = payload.get("target") or {}
    crossings = payload.get("crossings") or []
    if target.get("fra_id") != TARGET_FRA_ID or abs(float(target.get("milepost", -1)) - TARGET_MILEPOST) > 0.01:
        raise StaticConfigurationError("validated crossing config has the wrong target crossing")
    if len(crossings) != 8:
        raise StaticConfigurationError(f"validated crossing config has {len(crossings)} sentinels; expected 8")
    ids = [item.get("fra_id") for item in crossings]
    if len(set(ids)) != len(ids) or any(not item for item in ids):
        raise StaticConfigurationError("validated crossing config contains duplicate or empty FRA IDs")
    required_groups = {"Battle Creek", "Lansing", "Durand"}
    if {item.get("group") for item in crossings} != required_groups:
        raise StaticConfigurationError("validated crossing config does not cover all three boundary groups")
    for item in crossings:
        if not item.get("tomtom", {}).get("validated"):
            raise StaticConfigurationError(f"{item.get('fra_id')} is not marked as TomTom-validated")
        if not item.get("tile_mapping"):
            raise StaticConfigurationError(f"{item.get('fra_id')} has no static TomTom tile mapping")
    return payload


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_static_configuration(session: Session, path: str | Path = STATIC_CONFIG_PATH) -> dict:
    """Load checked-in setup data into the process-local runtime store.

    This intentionally performs no FRA or TomTom discovery.  A new static
    config is produced locally by ``train_tracker.cli init --live``.
    """
    payload = read_static_configuration(path)
    now = utc_now()
    selected_ids = set()
    for item in payload["crossings"]:
        fra_id = item["fra_id"]
        selected_ids.add(fra_id)
        crossing = session.scalar(select(Crossing).where(Crossing.fra_id == fra_id))
        if crossing is None:
            crossing = Crossing(fra_id=fra_id, created_at=now)
            session.add(crossing)
        crossing.name = item.get("name") or item.get("road_name") or fra_id
        crossing.group_name = item["group"]
        crossing.milepost = float(item["milepost"])
        crossing.latitude = float(item["latitude"])
        crossing.longitude = float(item["longitude"])
        crossing.railroad = item.get("railroad")
        crossing.subdivision = item.get("subdivision")
        crossing.aadt = item.get("aadt")
        crossing.aadt_year = item.get("aadt_year")
        crossing.fra_revision_date = _parse_datetime(item.get("fra_revision_date"))
        crossing.role = item["role"]
        crossing.poll_interval_sec = int(item["poll_interval_sec"])
        crossing.phase_sec = int(item["phase_sec"])
        crossing.tile_zoom = int(item["tile_zoom"])
        crossing.tile_mapping_json = item["tile_mapping"]
        crossing.coverage_score = item.get("coverage_score")
        crossing.enabled = True
        crossing.last_fra_sync_at = _parse_datetime(payload.get("generated_at")) or now
        crossing.updated_at = now

    for crossing in session.scalars(select(Crossing)).all():
        if crossing.group_name in {"Battle Creek", "Lansing", "Durand"} and crossing.fra_id not in selected_ids:
            crossing.enabled = False
            crossing.updated_at = now

    target_state = session.get(SystemState, "target_metadata")
    target_json = dict(payload["target"])
    if target_state is None:
        session.add(SystemState(key="target_metadata", value_json=target_json, updated_at=now))
    else:
        target_state.value_json = target_json
        target_state.updated_at = now
    session.flush()
    return payload
