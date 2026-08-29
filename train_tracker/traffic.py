from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import mapbox_vector_tile
import mercantile
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point

from .models import Crossing
from .tomtom import TileKey, TileResponse


@dataclass(frozen=True)
class FlowFeature:
    geometry: LineString
    properties: dict[str, Any]
    distance_m: float


@dataclass(frozen=True)
class TileMapping:
    zoom: int
    tiles: tuple[TileKey, ...]
    signatures: tuple[dict[str, Any], ...]
    max_distance_m: float = 75.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "zoom": self.zoom,
            "tiles": [{"z": t.z, "x": t.x, "y": t.y, "flow_type": t.flow_type} for t in self.tiles],
            "signatures": list(self.signatures),
            "max_distance_m": self.max_distance_m,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "TileMapping | None":
        if not value:
            return None
        tiles = tuple(TileKey(int(t["z"]), int(t["x"]), int(t["y"]), t.get("flow_type", "relative")) for t in value.get("tiles", []))
        return cls(int(value["zoom"]), tiles, tuple(value.get("signatures", [])), float(value.get("max_distance_m", 75)))


@dataclass(frozen=True)
class TrafficObservationResult:
    observed_at: datetime
    tile_fetched_at: datetime | None
    traffic_level_min: float | None
    traffic_level_median: float | None
    directional_values: dict[str, Any]
    road_coverage: str | None
    road_closure: bool | None
    feature_count: int
    usable: bool
    status: str
    error_detail: str | None = None


def crossing_tile(latitude: float, longitude: float, zoom: int) -> TileKey:
    tile = mercantile.tile(longitude, latitude, zoom)
    return TileKey(tile.z, tile.x, tile.y)


def _tile_coords_to_lonlat(coords: Iterable[tuple[float, float]], key: TileKey, extent: int) -> list[tuple[float, float]]:
    bounds = mercantile.bounds(mercantile.Tile(key.x, key.y, key.z))
    output = []
    for x, y in coords:
        lon = bounds.west + (float(x) / extent) * (bounds.east - bounds.west)
        # mapbox-vector-tile decodes standard MVT coordinates into a
        # bottom-left-oriented tile coordinate system by default.
        lat = bounds.south + (float(y) / extent) * (bounds.north - bounds.south)
        output.append((lon, lat))
    return output


def _geometry_lines(geometry: dict[str, Any], key: TileKey, extent: int) -> list[LineString]:
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "LineString":
        points = _tile_coords_to_lonlat(coords, key, extent)
        return [LineString(points)] if len(points) >= 2 else []
    if kind == "MultiLineString":
        return [LineString(_tile_coords_to_lonlat(line, key, extent)) for line in coords if len(line) >= 2]
    return []


def decode_flow_tile(body: bytes, key: TileKey) -> list[tuple[LineString, dict[str, Any]]]:
    decoded = mapbox_vector_tile.decode(body)
    output: list[tuple[LineString, dict[str, Any]]] = []
    for layer_name, layer in decoded.items():
        if "traffic" not in layer_name.lower() and layer_name.lower() not in {"flow", "traffic_flow"}:
            continue
        extent = int(layer.get("extent") or 4096)
        for feature in layer.get("features", []):
            properties = dict(feature.get("properties") or {})
            for line in _geometry_lines(feature.get("geometry") or {}, key, extent):
                output.append((line, properties))
    return output


def _metric_transformer(latitude: float, longitude: float) -> Transformer:
    zone = int((longitude + 180) / 6) + 1
    epsg = 32600 + zone if latitude >= 0 else 32700 + zone
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


def road_features_near_crossing(
    body: bytes,
    key: TileKey,
    latitude: float,
    longitude: float,
    max_distance_m: float = 75.0,
    signatures: Iterable[dict[str, Any]] | None = None,
) -> list[FlowFeature]:
    transformer = _metric_transformer(latitude, longitude)
    crossing_point = Point(*transformer.transform(longitude, latitude))
    wanted = list(signatures or [])
    candidates: list[FlowFeature] = []
    for line, properties in decode_flow_tile(body, key):
        metric_line = LineString([transformer.transform(x, y) for x, y in line.coords])
        distance = metric_line.distance(crossing_point)
        if distance <= max_distance_m:
            if wanted and not any(_signature_matches(properties, signature) for signature in wanted):
                # Keep a geometrically close feature if the provider changed a non-semantic road tag.
                if distance > max_distance_m * 0.55:
                    continue
            candidates.append(FlowFeature(line, properties, distance))
    return sorted(candidates, key=lambda item: item.distance_m)


def _signature_matches(properties: dict[str, Any], signature: dict[str, Any]) -> bool:
    for key in ("road_type", "road_category", "road_subcategory"):
        expected = signature.get(key)
        if expected is not None and properties.get(key) not in (expected, None):
            return False
    return True


def feature_signature(feature: FlowFeature) -> dict[str, Any]:
    props = feature.properties
    return {
        "road_type": props.get("road_type"),
        "road_category": props.get("road_category"),
        "road_subcategory": props.get("road_subcategory"),
        "distance_m": round(feature.distance_m, 2),
    }


def mapping_from_tile(
    body: bytes,
    key: TileKey,
    latitude: float,
    longitude: float,
    max_distance_m: float = 75.0,
) -> TileMapping | None:
    features = road_features_near_crossing(body, key, latitude, longitude, max_distance_m)
    if not features:
        return None
    best = features[:4]
    return TileMapping(key.z, (key,), tuple(feature_signature(feature) for feature in best), max_distance_m)


def _value(properties: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in properties and properties[key] is not None:
            return properties[key]
    return None


def _level(properties: dict[str, Any]) -> float | None:
    value = _value(properties, "traffic_level", "relative_speed", "realtive_speed")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, value))


def observation_from_tiles(
    crossing: Crossing,
    tile_responses: Iterable[TileResponse],
    observed_at: datetime | None = None,
) -> TrafficObservationResult:
    observed_at = observed_at or datetime.now(timezone.utc)
    mapping = TileMapping.from_dict(crossing.tile_mapping_json)
    all_features: list[FlowFeature] = []
    newest_fetch: datetime | None = None
    for response in tile_responses:
        newest_fetch = max(newest_fetch, response.fetched_at) if newest_fetch else response.fetched_at
        all_features.extend(
            road_features_near_crossing(
                response.body,
                response.key,
                crossing.latitude,
                crossing.longitude,
                mapping.max_distance_m if mapping else 75.0,
                mapping.signatures if mapping else None,
            )
        )
    levels = [level for feature in all_features if (level := _level(feature.properties)) is not None]
    coverage_values = [_value(feature.properties, "traffic_road_coverage") for feature in all_features]
    coverage = next((str(value) for value in coverage_values if value is not None), None)
    closures = [_value(feature.properties, "road_closure") for feature in all_features]
    closure = any(bool(value) for value in closures) if closures else None
    directional = {
        str(index): {
            "traffic_level": _level(feature.properties),
            "left_hand_traffic": _value(feature.properties, "left_hand_traffic"),
            "road_category": _value(feature.properties, "road_category", "road_type"),
        }
        for index, feature in enumerate(all_features)
    }
    if not all_features:
        return TrafficObservationResult(observed_at, newest_fetch, None, None, {}, None, None, 0, False, "NO_MATCHING_FLOW")
    if not levels:
        return TrafficObservationResult(observed_at, newest_fetch, None, None, directional, coverage, closure, len(all_features), False, "NO_TRAFFIC_LEVEL")
    return TrafficObservationResult(
        observed_at,
        newest_fetch,
        min(levels),
        statistics.median(levels),
        directional,
        coverage,
        closure,
        len(all_features),
        True,
        "OK",
    )
