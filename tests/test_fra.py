import pytest

from train_tracker.fra import FRAValidationError, validate_feature


def _feature(**overrides):
    attrs = {
        "CrossingID": "283602W", "RailroadCode": "GTW", "RailroadSubdivision": "Flint Subdivision",
        "CrossingType": "Public", "CrossingPosition": "At Grade", "CrossingPurpose": "Highway", "ReasonCode": "14",
        "NumberOfMainTracks": 1, "STREET": "Lawrence St", "RailroadMilepostNumber": "201.890",
        "Longitude": -84.84, "LATITUDE": 42.56, "AnnualAverageDailyTrafficCount": "1200", "AnnualAverageDailyTrafficYear": "2024",
    }
    attrs.update(overrides)
    return {"attributes": attrs}


def test_fra_authoritative_record_is_normalized():
    record = validate_feature(_feature())
    assert record.fra_id == "283602W"
    assert record.milepost == 201.89
    assert record.aadt == 1200


@pytest.mark.parametrize("overrides", [{"RailroadSubdivision": "Other Subdivision"}, {"CrossingPosition": "Overpass"}, {"CrossingType": "Private"}, {"CrossingPurpose": "Private"}, {"ReasonCode": "C"}, {"NumberOfMainTracks": 0}])
def test_invalid_fra_record_fails_closed(overrides):
    with pytest.raises(FRAValidationError):
        validate_feature(_feature(**overrides))
