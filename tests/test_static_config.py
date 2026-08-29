from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from train_tracker.crossings import load_static_configuration
from train_tracker.models import Base, Crossing, SystemState


def test_checked_in_config_loads_without_provider_discovery():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        payload = load_static_configuration(session)
        session.commit()
        crossings = list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())
        target = session.get(SystemState, "target_metadata")

    assert payload["setup_only"] is True
    assert len(crossings) == 8
    assert {crossing.group_name for crossing in crossings} == {"Battle Creek", "Lansing", "Durand"}
    assert all(crossing.tile_mapping_json for crossing in crossings)
    assert target.value_json["fra_id"] == "283602W"

