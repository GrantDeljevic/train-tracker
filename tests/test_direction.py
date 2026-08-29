from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from train_tracker.models import Crossing
from train_tracker.train_tracker import infer_group_sequences


BASE = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)


def _crossing(identifier, group, milepost):
    return Crossing(id=identifier, fra_id=str(identifier), name=str(identifier), group_name=group, milepost=milepost, latitude=42.5, longitude=-84.8, enabled=True)


def _event(identifier, crossing_id, minutes):
    return SimpleNamespace(id=identifier, crossing_id=crossing_id, event_time_estimate=BASE + timedelta(minutes=minutes), event_time_low=None, event_time_high=None)


def test_battle_creek_toward_charlotte():
    crossings = {1: _crossing(1, "Battle Creek", 181.160), 2: _crossing(2, "Battle Creek", 182.480)}
    result = infer_group_sequences([_event(10, 1, 0), _event(11, 2, 2)], crossings, "Battle Creek")
    assert any(item.direction == "from_battle_creek" and item.status == "APPROACHING" for item in result)


def test_battle_creek_reverse_is_away():
    crossings = {1: _crossing(1, "Battle Creek", 181.160), 2: _crossing(2, "Battle Creek", 182.480)}
    result = infer_group_sequences([_event(10, 2, 0), _event(11, 1, 2)], crossings, "Battle Creek")
    assert any(item.direction == "away_from_charlotte" and item.status == "PASSED/EXPIRED" for item in result)


def test_lansing_toward_charlotte_is_decreasing_milepost():
    crossings = {1: _crossing(1, "Lansing", 216.190), 2: _crossing(2, "Lansing", 215.410)}
    result = infer_group_sequences([_event(10, 1, 0), _event(11, 2, 1)], crossings, "Lansing")
    assert any(item.direction == "from_lansing" and item.status == "APPROACHING" for item in result)


def test_durand_three_crossing_sequence_is_high_confidence():
    crossings = {1: _crossing(1, "Durand", 253.030), 2: _crossing(2, "Durand", 251.730), 3: _crossing(3, "Durand", 248.650)}
    result = infer_group_sequences([_event(10, 1, 0), _event(11, 2, 3), _event(12, 3, 8)], crossings, "Durand")
    assert any(item.direction == "from_durand" and item.status == "HIGH_CONFIDENCE" and len(item.event_ids) == 3 for item in result)

