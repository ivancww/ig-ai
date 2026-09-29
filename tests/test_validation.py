from datetime import UTC, datetime, timedelta

import pytest

from ig_ai.research import score_bucket
from ig_ai.validation import (
    CalibrationInterface,
    OutcomeClassificationConfig,
    ValidationSplitConfig,
    atr_noise_threshold,
    classify_forward_outcome,
    classify_return,
    grouped_descriptive_metrics,
    overlap_metadata,
    research_labels,
    sample_warning,
    summarize_outcomes,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def test_score_bucket_covers_full_domain_and_boundaries_without_normalizing():
    assert score_bucket(0) == "0–10"
    assert score_bucket(9.99) == "0–10"
    assert score_bucket(10) == "10–20"
    assert score_bucket(49.99) == "40–50"
    assert score_bucket(50) == "50–55"
    assert score_bucket(55) == "55–60"
    assert score_bucket(100) == "90–100"
    assert score_bucket(-0.01) is None
    assert score_bucket(100.01) is None


def test_validation_split_is_chronological_and_enforces_purge_embargo():
    config = ValidationSplitConfig(
        development_start=START,
        development_end=START + timedelta(days=10),
        validation_start=START + timedelta(days=11),
        validation_end=START + timedelta(days=20),
        out_of_sample_start=START + timedelta(days=21),
        out_of_sample_end=START + timedelta(days=30),
        purge_embargo=timedelta(days=1),
    )
    assert config.period_for(START + timedelta(days=5)) == "DEVELOPMENT"
    assert config.period_for(START + timedelta(days=15)) == "VALIDATION"
    assert config.period_for(START + timedelta(days=25)) == "OUT_OF_SAMPLE"
    assert config.purge_boundary(START + timedelta(days=10), START + timedelta(days=11))
    with pytest.raises(ValueError):
        ValidationSplitConfig(
            development_start=START,
            development_end=START + timedelta(days=10),
            validation_start=START + timedelta(days=10, hours=12),
            validation_end=START + timedelta(days=20),
            purge_embargo=timedelta(days=1),
        )


def test_atr_threshold_scales_by_horizon_and_handles_missing_atr():
    config = OutcomeClassificationConfig(atr_multiplier=0.5, execution_noise_floor=0.001)
    one_hour = atr_noise_threshold(atr14=2, reference_price=100, horizon="1H", config=config)
    four_hour = atr_noise_threshold(atr14=2, reference_price=100, horizon="4H", config=config)
    assert one_hour == pytest.approx(0.01)
    assert four_hour == pytest.approx(0.02)
    assert atr_noise_threshold(atr14=None, reference_price=100, horizon="1H", config=config) is None
    assert classify_forward_outcome(reference_price=100, future_price=101, atr14=None, horizon="1H", config=config) is None


def test_horizon_classification_is_explicit_three_class_and_not_probability():
    assert classify_return(0.011, 0.01) == "UP"
    assert classify_return(-0.011, 0.01) == "DOWN"
    assert classify_return(0.01, 0.01) == "NEUTRAL"
    assert classify_return(None, 0.01) is None


def test_sample_warnings_and_descriptive_metrics_include_denominators():
    assert sample_warning(29) == "EXPLORATORY / UNSTABLE"
    assert sample_warning(30) == "EARLY SAMPLE WARNING"
    assert sample_warning(100) == "ENGINEERING BOOTSTRAP GATE MET"
    result = summarize_outcomes([
        {"status": "COMPLETE", "direction": "UP", "direction_outcome": "UP", "percentage_move": 1.0, "mfe": 2.0, "mae": -0.5, "time_to_reversal": None, "holding_window": "4–8H"},
        {"status": "COMPLETE", "direction": "DOWN", "direction_outcome": "FLAT", "percentage_move": 0.0, "mfe": 1.0, "mae": -1.0, "time_to_reversal": 60, "holding_window": "15–60M"},
    ])
    assert result["n"] == 2
    assert result["directional_hit_rate"] == 0.5
    assert result["outcome_counts"] == {"UP": 1, "FLAT": 1}
    assert result["sample_warning"] == "EXPLORATORY / UNSTABLE"
    assert grouped_descriptive_metrics([
        {"dimension": "A", "status": "COMPLETE", "direction": "UP", "direction_outcome": "UP"},
    ], "dimension")["A"]["n"] == 1


def test_overlap_metadata_and_calibration_interface_are_explicit():
    metadata = overlap_metadata([START, START + timedelta(hours=1), START + timedelta(hours=2)], horizon="4H", purge_embargo=timedelta(days=1))
    assert metadata.reference_count == 3
    assert metadata.overlapping_pairs == 2
    assert metadata.non_overlapping_reference_count == 1
    interface = CalibrationInterface()
    assert not interface.calibrated
    assert interface.brier_score is None
    assert research_labels()["probability"] == "NOT CALIBRATED PROBABILITY"
