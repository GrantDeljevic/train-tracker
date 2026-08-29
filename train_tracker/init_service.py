from __future__ import annotations

import logging

from sqlalchemy import select

from .crossings import CANDIDATE_SPECS, FIXED_SPECS, TARGET_FRA_ID, TARGET_NAME, CrossingManager, CrossingSpec, choose_battle_creek_candidate
from .db import SessionLocal, init_db, utc_now
from .fra import FRAClient, FRARecord
from .models import Crossing, SystemState
from .tomtom import TomTomClient
from .traffic import TileMapping, crossing_tile, mapping_from_tile
from .usage import UsageService

LOGGER = logging.getLogger(__name__)

BOUNDARY_RANGES = {
    "Battle Creek": (180.0, 200.0),
    "Lansing": (212.0, 220.0),
    "Durand": (245.0, 256.0),
}


def _adjacent(key):
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        yield type(key)(key.z, key.x + dx, key.y + dy, key.flow_type)


def probe_record(record: FRARecord, tomtom: TomTomClient, zooms=(16, 17, 18)) -> tuple[TileMapping | None, float | None]:
    """Find the lowest zoom that contains a usable flow geometry."""
    for zoom in zooms:
        containing = crossing_tile(record.latitude, record.longitude, zoom)
        response = tomtom.fetch_tile(containing)
        mapping = mapping_from_tile(response.body, containing, record.latitude, record.longitude)
        if mapping:
            score = max(0.0, min(1.0, 1.0 - mapping.signatures[0].get("distance_m", 75) / 75))
            return mapping, score
        for adjacent in _adjacent(containing):
            if adjacent.x < 0 or adjacent.y < 0 or adjacent.x >= 2**zoom or adjacent.y >= 2**zoom:
                continue
            response = tomtom.fetch_tile(adjacent)
            mapping = mapping_from_tile(response.body, adjacent, record.latitude, record.longitude)
            if mapping:
                score = max(0.0, min(1.0, 1.0 - mapping.signatures[0].get("distance_m", 75) / 75))
                return mapping, score
    return None, None


def _rank(record: FRARecord, coverage: dict[str, float | None]) -> tuple[float, int]:
    score = coverage.get(record.fra_id)
    # A successful, close road match is a coverage gate. Once it clears the
    # gate, AADT is the stronger initial quality prior; tiny distance-score
    # differences should not displace a materially busier public road.
    coverage_gate = 1.0 if score is not None and score >= 0.50 else 0.0
    return (coverage_gate, record.aadt or 0, score or 0.0)


def _spec_for(record: FRARecord, group_name: str) -> CrossingSpec:
    return CrossingSpec(record.fra_id, record.street.title(), group_name, record.milepost)


def _mapping_payload(mapping: TileMapping, score: float | None) -> dict:
    payload = mapping.as_dict()
    payload["geometry_score"] = score
    return payload


def initialize(live: bool = False) -> dict:
    init_db()
    fra = FRAClient()
    usage = UsageService(SessionLocal)
    tomtom = TomTomClient(usage_callback=usage.record, request_guard=usage.allowed) if live else None
    try:
        static_specs = FIXED_SPECS + CANDIDATE_SPECS
        target = fra.resolve_crossing(TARGET_FRA_ID)
        records = {spec.fra_id: fra.resolve_crossing(spec.fra_id) for spec in static_specs}
        coverage: dict[str, float | None] = {}
        mappings: dict[str, dict] = {}
        pools: dict[str, dict[str, FRARecord]] = {group: {} for group in BOUNDARY_RANGES}
        for spec in static_specs:
            pools[spec.group_name][spec.fra_id] = records[spec.fra_id]

        if live:
            # Discover each local FRA-maintained corridor once so a no-flow
            # sentinel can be replaced without user intervention.
            anchors = {"Battle Creek": records["283556X"], "Lansing": records["283632N"], "Durand": records["283716J"]}
            for group, anchor in anchors.items():
                low, high = BOUNDARY_RANGES[group]
                nearby = fra.find_valid_crossings_near(anchor.latitude, anchor.longitude, low, high)
                pools[group].update({item.fra_id: item for item in nearby})

            # Candidate selection is based on actual flow plus FRA AADT.
            candidate_pool = [record for record in pools["Battle Creek"].values() if 184.0 <= record.milepost <= 190.0]
            for record in candidate_pool:
                mapping, score = probe_record(record, tomtom)
                coverage[record.fra_id] = score
                if mapping:
                    mappings[record.fra_id] = _mapping_payload(mapping, score)
                LOGGER.info("TomTom probe %s: mapping=%s score=%s", record.fra_id, bool(mapping), score)
            for spec in FIXED_SPECS + CANDIDATE_SPECS:
                if spec.fra_id in coverage:
                    continue
                mapping, score = probe_record(records[spec.fra_id], tomtom)
                coverage[spec.fra_id] = score
                if mapping:
                    mappings[spec.fra_id] = _mapping_payload(mapping, score)
                LOGGER.info("TomTom probe %s: mapping=%s score=%s", spec.fra_id, bool(mapping), score)

            # Only broaden live probing where an explicitly configured
            # sentinel needs a replacement; this keeps initialization traffic
            # bounded while still honoring the automatic-substitution rule.
            missing_groups = {spec.group_name for spec in FIXED_SPECS if not mappings.get(spec.fra_id)}
            for group in missing_groups:
                for record in pools[group].values():
                    if record.fra_id in coverage:
                        continue
                    mapping, score = probe_record(record, tomtom)
                    coverage[record.fra_id] = score
                    if mapping:
                        mappings[record.fra_id] = _mapping_payload(mapping, score)
                    LOGGER.info("TomTom substitute probe %s: mapping=%s score=%s", record.fra_id, bool(mapping), score)

            chosen_candidates = [record for record in candidate_pool if mappings.get(record.fra_id)]
            if not chosen_candidates:
                raise RuntimeError("No usable TomTom flow geometry in Battle Creek MP 184-190 candidate corridor")
            chosen = max(chosen_candidates, key=lambda record: _rank(record, coverage))
            selected_records: dict[str, FRARecord] = {spec.fra_id: records[spec.fra_id] for spec in FIXED_SPECS if mappings.get(spec.fra_id)}
            selected_records[chosen.fra_id] = chosen
            replacement_records: dict[str, FRARecord] = {}
            replacement_specs: dict[str, CrossingSpec] = {}
            replaced_ids: set[str] = set()
            selected_ids = set(selected_records)
            for spec in FIXED_SPECS:
                if spec.fra_id in selected_records:
                    continue
                alternatives = [record for record in pools[spec.group_name].values() if record.fra_id not in selected_ids and mappings.get(record.fra_id)]
                if not alternatives:
                    raise RuntimeError(f"No usable same-corridor TomTom substitute for {spec.fra_id} ({spec.name})")
                replacement = max(alternatives, key=lambda record: _rank(record, coverage))
                replacement_records[replacement.fra_id] = replacement
                replacement_specs[replacement.fra_id] = _spec_for(replacement, spec.group_name)
                replaced_ids.add(spec.fra_id)
                selected_ids.add(replacement.fra_id)
                LOGGER.warning("Replacing unavailable %s with %s (%s)", spec.fra_id, replacement.fra_id, replacement.street)
            selected_ids = set(selected_records) | set(replacement_records)
            if len(selected_ids) != 8:
                raise RuntimeError(f"Live initialization selected {len(selected_ids)} unique sentinels; expected 8")
        else:
            chosen = choose_battle_creek_candidate([records[spec.fra_id] for spec in CANDIDATE_SPECS])
            replacement_records = {}
            replacement_specs = {}
            replaced_ids = set()

        manager = CrossingManager(fra)
        with SessionLocal() as session:
            manager.sync(session, coverage_scores=coverage, tile_configs=mappings, selected_record=chosen, replacement_specs=replacement_specs, replacement_records=replacement_records, exclude_fra_ids=replaced_ids)
            target_json = {
                "fra_id": target.fra_id,
                "name": TARGET_NAME,
                "street": target.street,
                "railroad": target.railroad,
                "subdivision": target.subdivision,
                "milepost": target.milepost,
                "latitude": target.latitude,
                "longitude": target.longitude,
                "aadt": target.aadt,
                "aadt_year": target.aadt_year,
                "revision_date": target.revision_date.isoformat() if target.revision_date else None,
            }
            target_state = session.get(SystemState, "target_metadata")
            if target_state is None:
                session.add(SystemState(key="target_metadata", value_json=target_json, updated_at=utc_now()))
            else:
                target_state.value_json = target_json
                target_state.updated_at = utc_now()
            session.commit()
            result = list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())
        return {"crossings": len(result), "selected_battle_creek": chosen.fra_id, "live_validated": live, "coverage": coverage}
    finally:
        fra.close()
        if tomtom:
            tomtom.close()
