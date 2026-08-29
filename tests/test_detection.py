from train_tracker.detection import classify_traffic


def test_missing_flow_is_unknown_not_clear():
    assert classify_traffic(None).severity == "UNKNOWN"


def test_abrupt_drop_is_weak_anomaly():
    decision = classify_traffic(0.70, previous=0.92, baseline=0.90)
    assert decision.severity == "WEAK"
    assert decision.drop == 0.22


def test_strong_level_is_anomaly_without_previous_poll():
    assert classify_traffic(0.20).severity == "STRONG"

