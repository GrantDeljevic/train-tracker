from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from train_tracker.calibration import calculate_crossing_quality
from train_tracker.models import Base, Crossing, CrossingEvent, TrainHypothesis, TrafficObservation
from train_tracker.train_tracker import refresh_hypotheses
from train_tracker.usage import UsageService


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

