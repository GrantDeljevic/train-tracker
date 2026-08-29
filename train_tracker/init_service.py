from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import BASE_DIR
from .crossings import (
    CANDIDATE_SPECS,
    FIXED_SPECS,
    STATIC_CONFIG_PATH,
    TARGET_FRA_ID,
    TARGET_NAME,
    CrossingSpec,
    choose_battle_creek_candidate,
)
from .fra import FRAClient, FRARecord, FRA_LAYER_URL
from .tomtom import TomTomClient
from .traffic import TileMapping, crossing_tile, mapping_from_tile

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


def _rank(record: FRARecord, coverage: dict[str, float | None]) -> tuple[float, int, float]:
    score = coverage.get(record.fra_id)
    # A successful close road match is a gate.  After that, AADT is the
    # stronger initial quality prior; distance-score differences are small.
    coverage_gate = 1.0 if score is not None and score >= 0.50 else 0.0
    return (coverage_gate, record.aadt or 0, score or 0.0)


def _spec_for(record: FRARecord, group_name: str) -> CrossingSpec:
    return CrossingSpec(record.fra_id, record.street.title(), group_name, record.milepost)


def _mapping_payload(mapping: TileMapping, score: float | None) -> dict:
    payload = mapping.as_dict()
    payload["geometry_score"] = score
    payload["validated_at_setup"] = True
    return payload


def _record_config(
    record: FRARecord,
    spec: CrossingSpec,
    mapping: dict,
    coverage_score: float | None,
    role: str,
    phase_sec: int,
    selection: dict,
) -> dict:
    return {
        "fra_id": record.fra_id,
        "name": spec.name,
        "road_name": record.street,
        "railroad": record.railroad,
        "subdivision": record.subdivision,
        "milepost": record.milepost,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "aadt": record.aadt,
        "aadt_year": record.aadt_year,
        "fra_revision_date": record.revision_date.isoformat() if record.revision_date else None,
        "group": spec.group_name,
        "role": role,
        "poll_interval_sec": 120 if role == "primary" else 240,
        "phase_sec": phase_sec,
        "tile_zoom": mapping["zoom"],
        "tile_mapping": mapping,
        "coverage_score": coverage_score,
        "tomtom": {
            "validated": True,
            "flow_type": "relative",
            "geometry_max_distance_m": mapping.get("max_distance_m", 75.0),
            "selected_road_geometry": mapping.get("signatures", []),
        },
        "selection": selection,
    }


def _build_static_payload(
    target: FRARecord,
    selected_records: dict[str, FRARecord],
    specs: dict[str, CrossingSpec],
    mappings: dict[str, dict],
    coverage: dict[str, float | None],
    substitutions: list[dict],
) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    crossing_rows = []
    for group in ("Battle Creek", "Lansing", "Durand"):
        group_records = [record for record in selected_records.values() if specs[record.fra_id].group_name == group]
        primary = max(group_records, key=lambda record: _rank(record, coverage))
        backups = sorted((record for record in group_records if record.fra_id != primary.fra_id), key=lambda record: record.milepost)
        for record in group_records:
            role = "primary" if record.fra_id == primary.fra_id else "backup"
            phase = 0 if role == "primary" else 60 + backups.index(record) * 120
            crossing_rows.append(
                _record_config(
                    record,
                    specs[record.fra_id],
                    mappings[record.fra_id],
                    coverage.get(record.fra_id),
                    role,
                    phase,
                    next((item for item in substitutions if item.get("selected_fra_id") == record.fra_id), {"configured_fra_id": record.fra_id, "decision": "selected"}),
                )
            )

    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "setup_only": True,
        "sources": {
            "fra_layer": FRA_LAYER_URL,
            "tomtom_endpoint": "v4 relative flow",
            "tomtom_probe_zooms": [16, 17, 18],
            "tomtom_matching_radius_m": 75.0,
        },
        "target": {
            "fra_id": target.fra_id,
            "name": TARGET_NAME,
            "road_name": target.street,
            "railroad": target.railroad,
            "subdivision": target.subdivision,
            "milepost": target.milepost,
            "latitude": target.latitude,
            "longitude": target.longitude,
            "aadt": target.aadt,
            "aadt_year": target.aadt_year,
            "fra_revision_date": target.revision_date.isoformat() if target.revision_date else None,
            "validated": True,
        },
        "crossings": sorted(crossing_rows, key=lambda item: (item["group"], item["milepost"])),
        "substitutions": substitutions,
    }


def write_static_configuration(payload: dict, path: str | Path = STATIC_CONFIG_PATH) -> Path:
    output = Path(path)
    if not output.is_absolute():
        output = BASE_DIR / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return output


def initialize(live: bool = False, output_path: str | Path | None = STATIC_CONFIG_PATH) -> dict:
    """Resolve and optionally live-validate setup data locally.

    Production does not call this function.  It loads the resulting checked-in
    JSON instead, so restarts never trigger broad FRA discovery.
    """
    fra = FRAClient()
    tomtom = TomTomClient() if live else None
    try:
        static_specs = FIXED_SPECS + CANDIDATE_SPECS
        target = fra.resolve_crossing(TARGET_FRA_ID)
        records = {spec.fra_id: fra.resolve_crossing(spec.fra_id) for spec in static_specs}
        coverage: dict[str, float | None] = {}
        mappings: dict[str, dict] = {}
        pools: dict[str, dict[str, FRARecord]] = {group: {} for group in BOUNDARY_RANGES}
        for spec in static_specs:
            pools[spec.group_name][spec.fra_id] = records[spec.fra_id]

        substitutions: list[dict] = []
        if live:
            anchors = {"Battle Creek": records["283556X"], "Lansing": records["283632N"], "Durand": records["283716J"]}
            for group, anchor in anchors.items():
                low, high = BOUNDARY_RANGES[group]
                nearby = fra.find_valid_crossings_near(anchor.latitude, anchor.longitude, low, high)
                pools[group].update({item.fra_id: item for item in nearby})

            candidate_pool = [record for record in pools["Battle Creek"].values() if 184.0 <= record.milepost <= 190.0]
            for record in candidate_pool:
                mapping, score = probe_record(record, tomtom)
                coverage[record.fra_id] = score
                if mapping:
                    mappings[record.fra_id] = _mapping_payload(mapping, score)
                LOGGER.info("TomTom probe %s: mapping=%s score=%s", record.fra_id, bool(mapping), score)
            for spec in static_specs:
                if spec.fra_id in coverage:
                    continue
                mapping, score = probe_record(records[spec.fra_id], tomtom)
                coverage[spec.fra_id] = score
                if mapping:
                    mappings[spec.fra_id] = _mapping_payload(mapping, score)
                LOGGER.info("TomTom probe %s: mapping=%s score=%s", spec.fra_id, bool(mapping), score)

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
            replacement_specs: dict[str, CrossingSpec] = {}
            selected_ids = set(selected_records)
            for spec in FIXED_SPECS:
                if spec.fra_id in selected_records:
                    continue
                alternatives = [record for record in pools[spec.group_name].values() if record.fra_id not in selected_ids and mappings.get(record.fra_id)]
                if not alternatives:
                    raise RuntimeError(f"No usable same-corridor TomTom substitute for {spec.fra_id} ({spec.name})")
                replacement = max(alternatives, key=lambda record: _rank(record, coverage))
                replacement_specs[replacement.fra_id] = _spec_for(replacement, spec.group_name)
                substitutions.append({
                    "configured_fra_id": spec.fra_id,
                    "configured_name": spec.name,
                    "selected_fra_id": replacement.fra_id,
                    "selected_name": replacement.street,
                    "reason": "configured crossing had no usable TomTom flow geometry during local setup",
                })
                selected_records[replacement.fra_id] = replacement
                selected_ids.add(replacement.fra_id)
                LOGGER.warning("Replacing unavailable %s with %s (%s)", spec.fra_id, replacement.fra_id, replacement.street)
            if len(selected_records) != 8:
                raise RuntimeError(f"Live initialization selected {len(selected_records)} unique sentinels; expected 8")
        else:
            chosen = choose_battle_creek_candidate([records[spec.fra_id] for spec in CANDIDATE_SPECS])
            selected_records = {spec.fra_id: records[spec.fra_id] for spec in FIXED_SPECS}
            selected_records[chosen.fra_id] = chosen
            replacement_specs = {}

        if not live:
            return {"crossings": len(selected_records), "selected_battle_creek": chosen.fra_id, "live_validated": False, "config_written": False}

        specs = {spec.fra_id: spec for spec in static_specs}
        specs.update(replacement_specs)
        payload = _build_static_payload(target, selected_records, specs, mappings, coverage, substitutions)
        output = write_static_configuration(payload, output_path) if output_path else None
        return {
            "crossings": len(selected_records),
            "selected_battle_creek": chosen.fra_id,
            "live_validated": True,
            "coverage": coverage,
            "config_path": str(output) if output else None,
            "substitutions": substitutions,
        }
    finally:
        fra.close()
        if tomtom:
            tomtom.close()
