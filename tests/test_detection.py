from train_tracker.detection import classify_traffic


def test_missing_flow_is_unknown_not_clear():
    assert classify_traffic(None).severity == "UNKNOWN"


def test_abrupt_drop_is_weak_anomaly():
    decision = classify_traffic(0.70, previous=0.92, baseline=0.90)
    assert decision.severity == "WEAK"
    assert decision.drop == 0.22
    assert decision.abrupt_drop is True


def test_abrupt_drop_at_configured_threshold_is_not_normal():
    decision = classify_traffic(0.80, previous=1.0, baseline=1.0)
    assert decision.severity == "WEAK"
    assert decision.abrupt_drop is True


def test_nearby_directional_collapse_is_weak_when_median_supports_it():
    decision = classify_traffic(
        0.867,
        previous=0.95,
        baseline=1.0,
        current_min=0.202,
        previous_min=1.0,
        baseline_min=1.0,
        feature_count=3,
    )
    assert decision.severity == "WEAK"
    assert decision.directional_collapse is True
    assert decision.min_drop == 0.798


def test_far_directional_outlier_does_not_create_signal_without_support():
    decision = classify_traffic(
        0.95,
        previous=0.95,
        baseline=0.95,
        current_min=0.869,
        previous_min=1.0,
        baseline_min=1.0,
        feature_count=3,
    )
    assert decision.severity == "NORMAL"
    assert decision.directional_collapse is False


def test_two_feature_directional_collapse_can_create_possible_signal():
    decision = classify_traffic(
        0.95,
        previous=0.95,
        baseline=1.0,
        current_min=0.20,
        previous_min=1.0,
        baseline_min=1.0,
        feature_count=2,
    )
    assert decision.severity == "WEAK"
    assert decision.directional_collapse is True


def test_strong_level_is_anomaly_without_previous_poll():
    assert classify_traffic(0.20).severity == "STRONG"
