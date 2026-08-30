from types import SimpleNamespace

import mapbox_vector_tile
import mercantile
from shapely.geometry import LineString, mapping

from train_tracker.tomtom import TileKey
from train_tracker.traffic import decode_flow_tile, representative_flow_level, road_features_near_crossing


def _tile_fixture():
    key0 = mercantile.tile(-84.84, 42.56, 16)
    key = TileKey(key0.z, key0.x, key0.y)
    bounds = mercantile.bounds(key0)
    line = LineString([(bounds.west, (bounds.north + bounds.south) / 2), (bounds.east, (bounds.north + bounds.south) / 2)])
    payload = {"name": "Traffic flow", "features": [{"geometry": mapping(line), "properties": {"traffic_level": 0.31, "road_type": "Major road"}}]}
    body = mapbox_vector_tile.encode(payload, default_options={"quantize_bounds": (bounds.west, bounds.south, bounds.east, bounds.north)})
    return key, bounds, body


def test_lat_lon_to_xyz_and_pbf_to_geographic_geometry():
    key, bounds, body = _tile_fixture()
    decoded = decode_flow_tile(body, key)
    assert len(decoded) == 1
    line, props = decoded[0]
    assert props["traffic_level"] == 0.31
    assert abs(line.centroid.y - (bounds.north + bounds.south) / 2) < 0.00001


def test_nearest_crossing_road_extraction():
    key, bounds, body = _tile_fixture()
    features = road_features_near_crossing(body, key, (bounds.north + bounds.south) / 2, (bounds.west + bounds.east) / 2)
    assert len(features) == 1
    assert features[0].distance_m < 1


def test_representative_flow_uses_two_nearest_directional_features():
    values = {
        "0": {"traffic_level": 0.91, "distance_m": 2.0},
        "1": {"traffic_level": 1.0, "distance_m": 4.0},
        "2": {"traffic_level": 0.15, "distance_m": 60.0},
    }
    assert representative_flow_level(values) == 0.91
