import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from train_tracker.calibration import calculate_crossing_quality
from train_tracker.models import Base, Crossing, CrossingEvent, TrainHypothesis, TrafficObservation
from train_tracker.scheduler import PollScheduler, phase_anchor
from train_tracker.train_tracker import refresh_hypotheses
from train_tracker.usage import UsageService, usage_month


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _crossing(identifier, group="Battle Creek", milepost=181.16):
    now = datetime.now(timezone.utc)
    return Crossing(id=identifier, fra_id=str(identifier), name=str(identifier), group_name=group, milepost=milepost, latitude=42.5, longitude=-84.8, created_at=now, updated_at=now, enabled=True, tile_mapping_json={"geometry_score": 1.0})


def test_hypotheses_survive_a_new_database_session():
    factory = _db()
    with factory() as session:
        first = _crossing(1, milepost=181.16)
        second = _crossing(2, milepost=182.48)
        session.add_all([first, second])
        base = datetime(2026, 1, 1, 7, tzinfo=timezone.utc)
        session.add_all([
            CrossingEvent(id=10, crossing_id=1, event_time_estimate=base, event_time_low=base, event_time_high=base + timedelta(minutes=1), severity="STRONG", evidence_json={}, created_at=base),
            CrossingEvent(id=11, crossing_id=2, event_time_estimate=base + timedelta(minutes=2), event_time_low=base + timedelta(minutes=2), event_time_high=base + timedelta(minutes=3), severity="STRONG", evidence_json={}, created_at=base),
        ])
        session.commit()
        refresh_hypotheses(session, base + timedelta(minutes=4))
        session.commit()
    with factory() as session:
        rows = list(session.scalars(select(TrainHypothesis)).all())
        assert any(row.direction == "from_battle_creek" for row in rows)


def test_quality_metrics_include_validity_baseline_and_hourly_usefulness():
    factory = _db()
    with factory() as session:
        crossing = _crossing(1)
        session.add(crossing)
        base = datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
        for index in range(10):
            session.add(TrafficObservation(crossing_id=1, observed_at=base + timedelta(minutes=index), traffic_level_median=0.9, traffic_level_min=0.9, feature_count=1, usable=index != 0, severity="NORMAL" if index else "UNKNOWN", status="OK"))
        session.flush()
        quality = calculate_crossing_quality(session, 1, base + timedelta(hours=1))
        assert quality["valid_flow_percentage"] == 0.9
        assert quality["typical_baseline"] == 0.9
        assert "03" in quality["hourly_usefulness"]


def test_monthly_hard_quota_blocks_the_next_request():
    factory = _db()
    usage = UsageService(factory, hard_budget=1, soft_budget=1)
    assert usage.allowed() is True
    usage.record(200, "http")
    assert usage.allowed() is False
    assert usage.snapshot()["actual_request_count"] == 1


def test_usage_restore_never_moves_a_counter_backwards():
    factory = _db()
    usage = UsageService(factory)
    usage.record(200, "http")
    usage.record(200, "http")

    usage.restore("2026-08", {"actual_request_count": 1, "successful_requests": 1})

    snapshot = usage.snapshot()
    assert snapshot["actual_request_count"] == 2
    assert snapshot["successful_requests"] == 2


def test_scheduled_runtime_snapshot_restores_polling_context():
    factory = _db()
    now = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)
    with factory() as session:
        session.add(_crossing(1, milepost=181.16))
        session.commit()

    scheduler = PollScheduler(object(), session_factory=factory, initial_poll_all=True)
    scheduler.restore_runtime_state({
        "version": 2,
        "last_polled": {"1": "2026-01-01T06:58:00+00:00"},
        "burst_until": {"Battle Creek": "2026-01-01T07:18:00+00:00"},
        "last_run": "2026-01-01T06:58:00+00:00",
        "observations": [{
            "fra_id": "1", "observed_at": "2026-01-01T06:58:00+00:00",
            "traffic_level_median": 0.9, "traffic_level_min": 0.9,
            "feature_count": 1, "usable": True, "severity": "NORMAL", "status": "OK",
        }],
        "events": [{
            "id": 10, "fra_id": "1", "event_time_estimate": "2026-01-01T06:57:00+00:00",
            "event_time_low": "2026-01-01T06:56:00+00:00", "event_time_high": "2026-01-01T06:58:00+00:00",
            "severity": "STRONG", "evidence_json": {}, "created_at": "2026-01-01T06:57:00+00:00",
        }],
    })

    assert scheduler.initial_poll_all is False
    assert scheduler.last_polled == {1: now - timedelta(minutes=2)}
    assert scheduler.burst_until["Battle Creek"] == now + timedelta(minutes=18)
    with factory() as session:
        assert session.scalar(select(TrafficObservation).where(TrafficObservation.crossing_id == 1)) is not None
        assert session.get(CrossingEvent, 10) is not None


def test_serverless_bootstrap_seeds_relative_cadence_from_phases():
    factory = _db()
    now = datetime.fromtimestamp(1_700_000_005, timezone.utc)
    crossings = [
        _crossing(1, group="Battle Creek", milepost=181.16),
        _crossing(2, group="Lansing", milepost=215.41),
        _crossing(3, group="Durand", milepost=248.65),
    ]
    for crossing in crossings:
        crossing.role = "primary"
        crossing.poll_interval_sec = 120
    with factory() as session:
        session.add_all(crossings)
        session.commit()

    scheduler = PollScheduler(object(), session_factory=factory, initial_poll_all=True)
    polled = []
    scheduler.poll_crossing = lambda crossing_id, now=None: polled.append(crossing_id) or True
    scheduler._save_system_state = lambda *args: None

    assert scheduler.poll_due(now) == 3
    assert polled == [1, 2, 3]
    assert scheduler.last_polled == {crossing.id: phase_anchor(crossing, now) for crossing in crossings}


def test_legacy_runtime_snapshot_is_reanchored_to_crossing_phase():
    factory = _db()
    with factory() as session:
        session.add_all([
            _crossing(1, group="Battle Creek", milepost=181.16),
            _crossing(2, group="Lansing", milepost=215.41),
            _crossing(3, group="Durand", milepost=248.65),
        ])
        session.commit()

    scheduler = PollScheduler(object(), session_factory=factory)
    scheduler.restore_runtime_state({
        "version": 1,
        "last_polled": {
            "1": "2026-01-01T07:00:00+00:00",
            "2": "2026-01-01T07:00:00+00:00",
            "3": "2026-01-01T07:00:00+00:00",
        },
    })

    assert len(set(scheduler.last_polled.values())) == 3
    polled = []
    scheduler.poll_crossing = lambda crossing_id, now=None: polled.append(crossing_id) or True
    scheduler._save_system_state = lambda *args: None
    poll_time = datetime(2026, 1, 1, 7, 5, tzinfo=timezone.utc)

    assert scheduler.poll_due(poll_time) == 3
    assert polled == [1, 2, 3]
    expected = {
        crossing.id: phase_anchor(crossing, poll_time)
        for crossing in (
            _crossing(1, group="Battle Creek", milepost=181.16),
            _crossing(2, group="Lansing", milepost=215.41),
            _crossing(3, group="Durand", milepost=248.65),
        )
    }
    assert scheduler.last_polled == expected


def test_runtime_snapshot_carries_the_monotonic_usage_checkpoint():
    factory = _db()
    usage = UsageService(factory)
    usage.record(200, "http")
    scheduler = PollScheduler(object(), session_factory=factory, usage_service=usage)

    state = scheduler._runtime_state(datetime.now(timezone.utc))

    assert state["usage"]["month"] == usage_month()
    assert state["usage"]["actual_request_count"] == 1


def test_runtime_snapshot_does_not_embed_historical_rows():
    factory = _db()
    now = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)
    with factory() as session:
        crossing = _crossing(1)
        session.add(crossing)
        for index in range(200):
            observed_at = now - timedelta(minutes=index)
            session.add(TrafficObservation(
                crossing_id=crossing.id,
                observed_at=observed_at,
                traffic_level_min=0.2,
                traffic_level_median=0.2,
                feature_count=1,
                usable=True,
                severity="STRONG",
                status="OK",
                error_detail="x" * 500,
            ))
        session.add(CrossingEvent(
            id=1,
            crossing_id=crossing.id,
            event_time_estimate=now,
            event_time_low=now - timedelta(minutes=1),
            event_time_high=now + timedelta(minutes=1),
            severity="STRONG",
            evidence_json={"detail": "x" * 500},
            created_at=now,
        ))
        session.commit()

    scheduler = PollScheduler(object(), session_factory=factory)
    state = scheduler._runtime_state(now)

    assert state["version"] == 4
    assert "observations" not in state
    assert "events" not in state
    assert len(json.dumps(state, separators=(",", ":"))) < 45_000


def test_runtime_snapshot_carries_compact_detector_context_and_health():
    factory = _db()
    with factory() as session:
        session.add(_crossing(1))
        session.commit()
    scheduler = PollScheduler(object(), session_factory=factory)
    valid_at = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)
    scheduler.last_valid_by_group["Battle Creek"] = valid_at
    scheduler.detector_state["1"] = {
        "previous": 1.0,
        "baseline": 0.98,
        "previous_min": 1.0,
        "baseline_min": 0.99,
        "previous_directional_values": {"0": {"traffic_level": 1.0, "distance_m": 2.0}},
        "last_observed_at": valid_at.isoformat(),
    }
    state = scheduler._runtime_state(valid_at)

    assert state["last_valid_by_group"]["Battle Creek"] == valid_at.isoformat()
    assert state["detector_state"]["1"]["previous_min"] == 1.0

    restored = PollScheduler(object(), session_factory=factory)
    restored.restore_runtime_state(state)
    assert restored.last_valid_by_group["Battle Creek"] == valid_at
    assert restored.detector_state["1"]["baseline"] == 0.98
