"""Statistical-validation foundations that do not tune or calibrate the model.

The helpers in this module are deliberately descriptive and configuration-first.
They operate on already persisted, point-in-time research records and never
turn a technical Direction Score into a probability.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import median
from typing import Any

from .history import HORIZONS
from .research import (
    DESCRIPTIVE_RESEARCH_LABEL,
    NOT_CALIBRATED_PROBABILITY_LABEL,
    NOT_OUT_OF_SAMPLE_LABEL,
)

SAMPLE_WARNING_EXPLORATORY = "EXPLORATORY / UNSTABLE"
SAMPLE_WARNING_EARLY = "EARLY SAMPLE WARNING"
SAMPLE_WARNING_BOOTSTRAP = "ENGINEERING BOOTSTRAP GATE MET"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("validation timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ValidationSplitConfig:
    """Chronological split plan for future walk-forward research."""

    development_start: datetime
    development_end: datetime
    validation_start: datetime | None = None
    validation_end: datetime | None = None
    out_of_sample_start: datetime | None = None
    out_of_sample_end: datetime | None = None
    purge_embargo: timedelta = timedelta(days=1)
    forward_test: bool = False

    def __post_init__(self) -> None:
        values = {
            "development_start": self.development_start,
            "development_end": self.development_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "out_of_sample_start": self.out_of_sample_start,
            "out_of_sample_end": self.out_of_sample_end,
        }
        for _name, value in values.items():
            if value is not None:
                _utc(value)
        if self.purge_embargo < timedelta(0):
            raise ValueError("purge_embargo must be non-negative")
        if self.development_end <= self.development_start:
            raise ValueError("development period must be ordered")
        periods = (
            ("validation", self.validation_start, self.validation_end),
            ("out_of_sample", self.out_of_sample_start, self.out_of_sample_end),
        )
        for name, start, end in periods:
            if (start is None) != (end is None):
                raise ValueError(f"{name} period requires both start and end")
            if start is not None and end is not None and end <= start:
                raise ValueError(f"{name} period must be ordered")
        if self.validation_start and self.validation_start < self.development_end + self.purge_embargo:
            raise ValueError("validation period violates development purge/embargo")
        if self.out_of_sample_start:
            prior_end = self.validation_end or self.development_end
            if self.out_of_sample_start < prior_end + self.purge_embargo:
                raise ValueError("out-of-sample period violates purge/embargo")

    def period_for(self, timestamp: datetime) -> str | None:
        timestamp = _utc(timestamp)
        if self.development_start <= timestamp < self.development_end:
            return "DEVELOPMENT"
        if self.validation_start and self.validation_start <= timestamp < self.validation_end:
            return "VALIDATION"
        if self.out_of_sample_start and self.out_of_sample_start <= timestamp < self.out_of_sample_end:
            return "OUT_OF_SAMPLE"
        return "FORWARD_TEST" if self.forward_test else None

    def purge_boundary(self, prior_end: datetime, next_start: datetime) -> bool:
        return _utc(next_start) >= _utc(prior_end) + self.purge_embargo


@dataclass(frozen=True)
class OutcomeClassificationConfig:
    """Pre-registered outcome-classification parameters.

    ``atr_multiplier`` is a declared engineering default, not a fitted value.
    It must be frozen before any calibration or threshold comparison.
    """

    atr_multiplier: float = 0.5
    execution_noise_floor: float = 0.001
    missing_atr_policy: str = "PENDING"
    legacy_descriptive_threshold: float = 0.001

    def __post_init__(self) -> None:
        if self.atr_multiplier <= 0 or self.execution_noise_floor < 0:
            raise ValueError("ATR multiplier must be positive and noise floor non-negative")
        if self.missing_atr_policy not in {"PENDING", "EXCLUDE"}:
            raise ValueError("missing_atr_policy must be PENDING or EXCLUDE")


def horizon_minutes(horizon: str) -> int:
    if horizon not in HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")
    return {"15M": 15, "1H": 60, "2H": 120, "4H": 240, "8H": 480, "1D": 1440}[horizon]


def atr_noise_threshold(*, atr14: float | None, reference_price: float, horizon: str, config: OutcomeClassificationConfig) -> float | None:
    """Return a return-unit threshold, or None when ATR is unavailable."""
    if atr14 is None or atr14 <= 0 or reference_price <= 0:
        return None
    scaled = config.atr_multiplier * (atr14 / reference_price) * sqrt(horizon_minutes(horizon) / 60)
    return max(config.execution_noise_floor, scaled)


def classify_return(forward_return: float | None, threshold: float | None) -> str | None:
    if forward_return is None or threshold is None:
        return None
    if forward_return > threshold:
        return "UP"
    if forward_return < -threshold:
        return "DOWN"
    return "NEUTRAL"


def classify_forward_outcome(*, reference_price: float, future_price: float | None, atr14: float | None, horizon: str, config: OutcomeClassificationConfig) -> str | None:
    threshold = atr_noise_threshold(atr14=atr14, reference_price=reference_price, horizon=horizon, config=config)
    if future_price is None or threshold is None:
        return None
    return classify_return((future_price - reference_price) / reference_price, threshold)


def sample_warning(sample_count: int) -> str:
    if sample_count < 30:
        return SAMPLE_WARNING_EXPLORATORY
    if sample_count < 100:
        return SAMPLE_WARNING_EARLY
    return SAMPLE_WARNING_BOOTSTRAP


def _safe_mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_outcomes(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calculate descriptive metrics with explicit denominators."""
    complete = [row for row in rows if row.get("status", "COMPLETE") == "COMPLETE"]
    n = len(complete)
    actual = Counter(row.get("direction_outcome") for row in complete)
    predictions = [row.get("predicted_direction", row.get("direction")) for row in complete]
    predicted = Counter(predictions)
    classes = ("UP", "DOWN", "NEUTRAL")
    recalls = []
    precisions: dict[str, float | None] = {}
    for label in classes:
        true_positive = sum(prediction == label and row.get("direction_outcome") == label for prediction, row in zip(predictions, complete, strict=True))
        actual_count = actual[label]
        predicted_count = predicted[label]
        recalls.append(true_positive / actual_count if actual_count else None)
        precisions[label] = true_positive / predicted_count if predicted_count else None
    valid_recalls = [value for value in recalls if value is not None]
    returns = [float(row["percentage_move"]) for row in complete if row.get("percentage_move") is not None]
    mfe = [float(row["mfe"]) for row in complete if row.get("mfe") is not None]
    mae = [float(row["mae"]) for row in complete if row.get("mae") is not None]
    reversals = [row for row in complete if row.get("time_to_reversal") is not None]
    holding_window_metrics: dict[Any, dict[str, Any]] = {}
    for window in sorted({row.get("holding_window") for row in complete}, key=str):
        group = [row for row in complete if row.get("holding_window") == window]
        holding_window_metrics[window] = {
            "n": len(group),
            "mean_forward_return": _safe_mean([
                float(row["percentage_move"]) for row in group if row.get("percentage_move") is not None
            ]),
        }
    return {
        "n": n,
        "sample_warning": sample_warning(n),
        "directional_hit_rate": sum(prediction == row.get("direction_outcome") for prediction, row in zip(predictions, complete, strict=True)) / n if n else None,
        "balanced_accuracy": sum(valid_recalls) / len(valid_recalls) if valid_recalls else None,
        "up_precision": precisions["UP"],
        "down_precision": precisions["DOWN"],
        "neutral_precision": precisions["NEUTRAL"],
        "neutral_coverage": actual["NEUTRAL"] / n if n else None,
        "mean_forward_return": _safe_mean(returns),
        "median_forward_return": median(returns) if returns else None,
        "mfe_mean": _safe_mean(mfe),
        "mae_mean": _safe_mean(mae),
        "continuation_rate": (n - len(reversals)) / n if n else None,
        "reversal_rate": len(reversals) / n if n else None,
        "holding_window_metrics": holding_window_metrics,
        "outcome_counts": dict(actual),
    }


def grouped_descriptive_metrics(rows: Iterable[dict[str, Any]], dimension: str) -> dict[Any, dict[str, Any]]:
    """Return the same descriptive metrics split by one persisted dimension."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get(dimension), []).append(row)
    return {key: summarize_outcomes(group) for key, group in sorted(groups.items(), key=lambda item: str(item[0]))}


@dataclass(frozen=True)
class OverlapMetadata:
    horizon: str
    reference_count: int
    distinct_reference_anchors: int
    overlapping_pairs: int
    non_overlapping_reference_count: int
    purge_embargo: timedelta


def overlap_metadata(reference_times: Iterable[datetime], *, horizon: str, purge_embargo: timedelta = timedelta(0)) -> OverlapMetadata:
    references = list(reference_times)
    ordered = sorted({_utc(value) for value in references})
    duration = timedelta(minutes=horizon_minutes(horizon))
    overlapping_pairs = sum(
        current < previous + duration for previous, current in zip(ordered, ordered[1:], strict=False)
    )
    selected: list[datetime] = []
    for timestamp in ordered:
        if not selected or timestamp >= selected[-1] + duration + purge_embargo:
            selected.append(timestamp)
    return OverlapMetadata(horizon, len(references), len(ordered), overlapping_pairs, len(selected), purge_embargo)


@dataclass(frozen=True)
class CalibrationInterface:
    """Schema for future calibration; no probabilities are accepted yet."""

    calibrated: bool = False
    reliability_curve: None = None
    brier_score: None = None
    log_loss: None = None
    calibration_slope: None = None
    calibration_intercept: None = None
    expected_calibration_error: None = None


def research_labels() -> dict[str, str]:
    return {
        "research": DESCRIPTIVE_RESEARCH_LABEL,
        "probability": NOT_CALIBRATED_PROBABILITY_LABEL,
        "performance": NOT_OUT_OF_SAMPLE_LABEL,
    }
