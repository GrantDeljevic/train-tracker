from datetime import datetime, timedelta, timezone

import httpx

from train_tracker.models import Crossing
from train_tracker.crossings import CrossingManager
from train_tracker.scheduler import due_crossings, interval_for
from train_tracker.tomtom import TileKey, TomTomClient


def _crossing(identifier, role="backup", group="Battle Creek", phase=0):
    return Crossing(id=identifier, fra_id=str(identifier), name="x", group_name=group, milepost=180 + identifier, latitude=42.5, longitude=-84.8, role=role, poll_interval_sec=120 if role == "primary" else 240, phase_sec=phase, enabled=True)


def test_primary_and_backup_intervals_and_staggered_due_times():
    primary = _crossing(1, "primary", phase=0)
    backup = _crossing(2, "backup", phase=120)
    now = datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert interval_for(primary) == 120
    assert interval_for(backup) == 240
    assert due_crossings([primary, backup], now, {1: now - timedelta(seconds=120), 2: now - timedelta(seconds=240)}) == [primary, backup]


def test_burst_changes_all_group_crossings_to_one_minute():
    crossings = [_crossing(1, "primary", phase=0), _crossing(2, "backup", phase=0)]
    now = datetime.now(timezone.utc)
    assert interval_for(crossings[0], burst=True) == 60
    assert interval_for(crossings[1], burst=True) == 60
    assert len(due_crossings(crossings, now, {1: now - timedelta(seconds=61), 2: now - timedelta(seconds=61)}, {"Battle Creek"})) == 2


def test_three_crossing_group_phases_are_spaced():
    crossings = [_crossing(1, "backup", phase=0), _crossing(2, "backup", phase=0), _crossing(3, "backup", phase=0)]
    for index, crossing in enumerate(crossings):
        crossing.coverage_score = 0.9
        crossing.aadt = 1000 + index
    session = type("Session", (), {"scalars": lambda self, statement: type("Result", (), {"all": lambda self: crossings})()})()
    CrossingManager.assign_roles(session)
    assert sum(crossing.role == "primary" for crossing in crossings) == 1
    assert sorted(crossing.phase_sec for crossing in crossings) == [0, 60, 180]


def test_tile_cache_deduplicates_actual_http_calls():
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=b"pbf", request=request)
    client = TomTomClient(api_key="test", client=httpx.Client(transport=httpx.MockTransport(handler)), cache_seconds=55)
    key = TileKey(16, 1, 2)
    first = client.fetch_tile(key)
    second = client.fetch_tile(key)
    assert first.body == second.body == b"pbf"
    assert len(calls) == 1
    assert second.from_cache is True
