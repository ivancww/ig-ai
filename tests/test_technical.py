from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sqrt

import pytest

from ig_ai.database import Database
from ig_ai.models import Candle
from ig_ai.technical import TechnicalConfig, TechnicalFeatureEngine, atr, ema, rsi


def candle(index: int, *, close: float, open_: float | None = None, high: float | None = None, low: float | None = None, closed: bool = True) -> Candle:
    start = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    open_price = close if open_ is None else open_
    return Candle(
        "TEST", "TEST.EPIC", "1H", start, start + timedelta(hours=1),
        Decimal(str(open_price)), Decimal(str(high if high is not None else max(open_price, close) + 1)),
        Decimal(str(low if low is not None else min(open_price, close) - 1)), Decimal(str(close)),
        is_closed=closed, observation_count=1,
    )


def test_ema_is_sma_seeded_and_deterministic():
    assert ema([1.0, 2.0, 3.0, 4.0], 2) == [None, 1.5, 2.5, 3.5]
    assert ema([1.0, 2.0], 3) == [None, None]


def test_rsi_uses_wilder_smoothing():
    values = [44, 45, 46, 45, 44, 45, 46, 47, 46, 45, 46]
    result = rsi(values, 5)
    assert result[5] == pytest.approx(60.0)
    assert result[6] == pytest.approx(68.0)


def test_atr_uses_true_range_and_wilder_smoothing():
    candles = [
        candle(0, close=10, high=11, low=9),
        candle(1, close=12, high=13, low=10),
        candle(2, close=11, high=12, low=9),
    ]
    values = atr(candles, 2)
    assert values[1] == pytest.approx(2.5)
    assert values[2] == pytest.approx(2.75)


def test_first_atr_has_no_recent_history():
    candles = [candle(i, close=100 + i, high=101 + i, low=99 + i) for i in range(14)]
    feature = TechnicalFeatureEngine().calculate(candles)
    assert feature["atr14"]["value"] == pytest.approx(2.0)
    assert feature["atr14"]["recent_comparison"] == "insufficient_history"


def test_feature_record_contains_numerically_checkable_price_geometry_and_bands():
    candles = [candle(i, close=100 + i, open_=99 + i) for i in range(25)]
    feature = TechnicalFeatureEngine().calculate(candles)
    assert feature["moving_averages"]["ema10"]["value"] == pytest.approx(124.0 - 4.5)
    assert feature["bollinger"]["middle"] == pytest.approx(114.5)
    assert feature["bollinger"]["width"] == pytest.approx(4 * sqrt(sum((x - 114.5) ** 2 for x in range(105, 125)) / 20))
    assert feature["candle"]["body_size"] == pytest.approx(1)
    assert feature["candle"]["total_range"] == pytest.approx(3)
    assert feature["candle"]["direction"] == "bullish"
    assert feature["candle"]["body_range_ratio"] == pytest.approx(1 / 3)


def test_macd_does_not_create_signal_before_slow_ema_plus_signal_period():
    short = [candle(i, close=100 + i) for i in range(33)]
    feature = TechnicalFeatureEngine().calculate(short)
    assert feature["macd"]["line"] is not None
    assert feature["macd"]["signal"] is None
    feature = TechnicalFeatureEngine().calculate(short + [candle(33, close=133)])
    assert feature["macd"]["signal"] is not None


def test_macd_histogram_state_uses_only_current_and_prior_available_values():
    candles = [candle(i, close=100 + i) for i in range(36)]
    first_signal = TechnicalFeatureEngine().calculate(candles, target_index=33)
    next_signal = TechnicalFeatureEngine().calculate(candles, target_index=34)
    assert first_signal["macd"]["histogram_state"] == "insufficient_history"
    assert next_signal["macd"]["histogram_state"] in {"strengthening", "weakening", "unchanged"}


def test_swing_confirmation_and_labels_are_explicit():
    closes = [10, 12, 9, 13, 10, 11, 14, 10, 11]
    candles = [candle(i, close=value, high=value + 0.25, low=value - 0.25) for i, value in enumerate(closes)]
    feature = TechnicalFeatureEngine().calculate(candles)
    assert feature["structure"]["confirmed_highs"][0]["classification"] is None
    assert feature["structure"]["confirmed_highs"][1]["classification"] == "HH"
    assert "candidate_high" in feature["structure"]


def test_forming_right_candle_cannot_confirm_pivot_and_later_invalidation_is_not_retroactive():
    forming = [
        candle(0, close=10, high=10, low=9),
        candle(1, close=12, high=12, low=11),
        candle(2, close=11, high=11, low=10, closed=False),
    ]
    invalidated = [*forming[:2], candle(2, close=13, high=13, low=10, closed=True)]
    engine = TechnicalFeatureEngine()
    assert engine.calculate(forming)["structure"]["confirmed_highs"] == []
    assert engine.calculate(invalidated)["structure"]["confirmed_highs"] == []


@pytest.mark.parametrize(
    ("close", "expected"),
    [(12.0, "above_upper"), (11.2, "upper_zone"), (10.8, "middle_zone"), (10.1, "lower_zone"), (9.0, "below_lower")],
)
def test_bollinger_has_explicit_configurable_zones(close, expected):
    candles = [candle(0, close=10), candle(1, close=11), candle(2, close=close)]
    config = TechnicalConfig(bollinger_period=3, bollinger_stddevs=1.0)
    assert TechnicalFeatureEngine(config).calculate(candles)["bollinger"]["zone"] == expected


def test_gaps_are_open_until_a_later_candle_fills_them_and_are_target_bounded():
    candles = [
        candle(0, close=10, high=11, low=9),
        candle(1, close=13, high=14, low=12),
        candle(2, close=12, high=13, low=10),
    ]
    engine = TechnicalFeatureEngine()
    open_feature = engine.calculate(candles, target_index=1)
    closed_feature = engine.calculate(candles, target_index=2)
    assert open_feature["gaps"][0]["open"] is True
    assert closed_feature["gaps"][0]["filled"] is True
    assert closed_feature["gaps"][0]["filled_at"] == candles[2].start.isoformat()


def test_no_lookahead_changes_nothing_before_target():
    history = [candle(i, close=100 + i) for i in range(30)]
    later = history + [candle(30, close=1000, high=1001, low=999)]
    engine = TechnicalFeatureEngine()
    assert engine.calculate(history)["rsi14"] == engine.calculate(later, target_index=29)["rsi14"]
    assert engine.calculate(history)["moving_averages"] == engine.calculate(later, target_index=29)["moving_averages"]


def test_forming_and_closed_states_share_identity_but_closed_replaces_forming(tmp_path):
    database = Database(tmp_path / "features.sqlite")
    forming = candle(0, close=100, open_=99, closed=False)
    closed = candle(0, close=101, open_=99, closed=True)
    database.save_candle(forming)
    database.save_features_for_candle(forming)
    database.save_candle(closed)
    database.save_features_for_candle(closed)
    count = database.connection.execute("SELECT COUNT(*) FROM technical_features").fetchone()[0]
    assert count == 1
    assert database.get_technical_features("TEST", "1H")[0]["candle_state"] == "CLOSED"
    database.close()
    reopened = Database(tmp_path / "features.sqlite")
    assert reopened.get_technical_features("TEST", "1H")[0]["schema_version"] == "technical_features_v1"
    reopened.close()


def test_outcome_foundation_allows_unavailable_future_values(tmp_path):
    database = Database(tmp_path / "outcomes.sqlite")
    database.save_outcome(instrument_id="TEST", timeframe="1H", feature_candle_start="2025-01-01T00:00:00+00:00", horizon="4H", reference_price="100")
    row = database.connection.execute("SELECT future_price, percentage_move FROM technical_outcomes").fetchone()
    assert row == (None, None)
    database.close()
