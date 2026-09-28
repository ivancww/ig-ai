"""Deterministic, target-bounded technical features.

Gap semantics are deliberately simple: a gap is measured between adjacent
completed candles in the same timeframe (15M, 1H, 4H, or 1D).  Session
boundaries are not inferred here, and Phase 2A does not classify breakaway,
runaway, exhaustion, or island-reversal gaps.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from typing import Any

from .models import Candle

FEATURE_SCHEMA_VERSION = "technical_features_v1"


@dataclass(frozen=True)
class TechnicalConfig:
    rsi_oversold: float = 30.0
    rsi_weak: float = 45.0
    rsi_strong: float = 55.0
    rsi_overbought: float = 70.0
    bollinger_period: int = 20
    bollinger_stddevs: float = 2.0
    bollinger_upper_zone_position: float = 0.75
    bollinger_lower_zone_position: float = 0.25
    atr_period: int = 14
    atr_comparison_period: int = 20
    swing_left: int = 1
    swing_right: int = 1


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 10)


def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        current += multiplier * (values[index] - current)
        result[index] = current
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("RSI period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value() -> float:
        if average_loss == 0:
            return 100.0 if average_gain else 50.0
        return 100 - 100 / (1 + average_gain / average_loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        average_gain = (average_gain * (period - 1) + gains[index - 1]) / period
        average_loss = (average_loss * (period - 1) + losses[index - 1]) / period
        result[index] = value()
    return result


def true_ranges(candles: list[Candle]) -> list[float]:
    result = []
    for index, candle in enumerate(candles):
        previous_close = float(candles[index - 1].close) if index else float(candle.close)
        result.append(max(float(candle.high) - float(candle.low), abs(float(candle.high) - previous_close), abs(float(candle.low) - previous_close)))
    return result


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    values = true_ranges(candles)
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    for index in range(period, len(values)):
        current = (current * (period - 1) + values[index]) / period
        result[index] = current
    return result


def _rsi_state(value: float | None, config: TechnicalConfig) -> str | None:
    if value is None:
        return None
    if value <= config.rsi_oversold:
        return "oversold"
    if value < config.rsi_weak:
        return "weak"
    if value <= config.rsi_strong:
        return "neutral"
    if value < config.rsi_overbought:
        return "strong"
    return "overbought"


def _macd(values: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast, slow = ema(values, 12), ema(values, 26)
    line = [None if a is None or b is None else a - b for a, b in zip(fast, slow, strict=True)]
    signal: list[float | None] = [None] * len(line)
    first = next((i for i, value in enumerate(line) if value is not None), len(line))
    signal[first:] = ema([value for value in line[first:] if value is not None], 9)
    histogram = [None if a is None or b is None else a - b for a, b in zip(line, signal, strict=True)]
    return line, signal, histogram


def _structure(candles: list[Candle], config: TechnicalConfig) -> dict[str, Any]:
    left, right = config.swing_left, config.swing_right
    if left <= 0 or right < 0:
        raise ValueError("invalid swing configuration")
    highs, lows = [], []
    for index in range(left, len(candles) - right):
        current = candles[index]
        right_side = candles[index + 1:index + right + 1]
        # A forming right-side candle cannot confirm a pivot.  It remains a
        # candidate until every required right-side candle is closed.
        if all(c.is_closed for c in right_side):
            if float(current.high) > max(float(c.high) for c in candles[index - left:index]) and (not right_side or float(current.high) >= max(float(c.high) for c in right_side)):
                highs.append({"timestamp": current.start.isoformat(), "price": _round(float(current.high)), "confirmed_at": right_side[-1].start.isoformat() if right_side else current.start.isoformat()})
            if float(current.low) < min(float(c.low) for c in candles[index - left:index]) and (not right_side or float(current.low) <= min(float(c.low) for c in right_side)):
                lows.append({"timestamp": current.start.isoformat(), "price": _round(float(current.low)), "confirmed_at": right_side[-1].start.isoformat() if right_side else current.start.isoformat()})

    def label(points: list[dict[str, Any]], higher: str, lower: str) -> list[dict[str, Any]]:
        previous = None
        output = []
        for point in points:
            classification = None if previous is None else higher if point["price"] > previous else lower if point["price"] < previous else "EQ"
            output.append({**point, "classification": classification})
            previous = point["price"]
        return output

    candidate_high = candidate_low = None
    if not candles[-1].is_closed and len(candles) > left:
        previous = candles[-left - 1:-1]
        candidate_high = _round(float(candles[-1].high)) if float(candles[-1].high) > max(float(c.high) for c in previous) else None
        candidate_low = _round(float(candles[-1].low)) if float(candles[-1].low) < min(float(c.low) for c in previous) else None
    return {"confirmed_highs": label(highs, "HH", "LH")[-10:], "confirmed_lows": label(lows, "HL", "LL")[-10:], "candidate_high": candidate_high, "candidate_low": candidate_low}


def _gaps(candles: list[Candle]) -> list[dict[str, Any]]:
    result = []
    for index in range(1, len(candles)):
        previous, current = candles[index - 1], candles[index]
        if float(current.low) > float(previous.high):
            direction, size, gap_low, gap_high = "up", float(current.low) - float(previous.high), float(previous.high), float(current.low)
        elif float(current.high) < float(previous.low):
            direction, size, gap_low, gap_high = "down", float(previous.low) - float(current.high), float(current.high), float(previous.low)
        else:
            continue
        filled_at = next((later.start.isoformat() for later in candles[index + 1:] if (direction == "up" and float(later.low) <= gap_low) or (direction == "down" and float(later.high) >= gap_high)), None)
        result.append({"timestamp": current.start.isoformat(), "direction": direction, "size": _round(size), "percentage": _round(size / float(previous.close) * 100), "open": filled_at is None, "filled": filled_at is not None, "filled_at": filled_at})
    return result[-20:]


class TechnicalFeatureEngine:
    def __init__(self, config: TechnicalConfig | None = None):
        self.config = config or TechnicalConfig()

    def calculate(self, candles: list[Candle], *, target_index: int | None = None) -> dict[str, Any]:
        if not candles:
            raise ValueError("at least one candle is required")
        target_index = len(candles) - 1 if target_index is None else target_index
        if not 0 <= target_index < len(candles):
            raise IndexError("target_index outside candle history")
        history, target = candles[:target_index + 1], candles[target_index]
        closes = [float(c.close) for c in history]
        averages = {period: ema(closes, period) for period in (10, 20, 50, 100, 200)}
        line, signal, histogram = _macd(closes)
        rsi_values, atr_values = rsi(closes), atr(history, self.config.atr_period)
        middle, upper, lower = [None] * len(closes), [None] * len(closes), [None] * len(closes)
        period = self.config.bollinger_period
        for index in range(period - 1, len(closes)):
            window = closes[index - period + 1:index + 1]
            mean = sum(window) / period
            deviation = sqrt(sum((x - mean) ** 2 for x in window) / period)
            middle[index], upper[index], lower[index] = mean, mean + self.config.bollinger_stddevs * deviation, mean - self.config.bollinger_stddevs * deviation
        rsi_value, atr_value = rsi_values[-1], atr_values[-1]
        position = None if upper[-1] is None or upper[-1] == lower[-1] else (closes[-1] - lower[-1]) / (upper[-1] - lower[-1])
        zone = None if position is None else "above_upper" if closes[-1] > upper[-1] else "below_lower" if closes[-1] < lower[-1] else "upper_zone" if position >= self.config.bollinger_upper_zone_position else "lower_zone" if position <= self.config.bollinger_lower_zone_position else "middle_zone"
        structure = _structure(history, self.config)
        prior_atr = [value for value in atr_values[:-1] if value is not None][-self.config.atr_comparison_period:]
        recent_mean = sum(prior_atr) / len(prior_atr) if prior_atr else None
        atr_comparison = "insufficient_history" if atr_value is None or recent_mean is None else "higher_than_recent" if atr_value > recent_mean else "lower_than_recent" if atr_value < recent_mean else "equal_to_recent"
        current_histogram, prior_histograms = histogram[-1], [value for value in histogram[:-1] if value is not None]
        histogram_state = "insufficient_history" if current_histogram is None or not prior_histograms else "strengthening" if abs(current_histogram) > abs(prior_histograms[-1]) else "weakening" if abs(current_histogram) < abs(prior_histograms[-1]) else "unchanged"
        candle_range, body = float(target.high - target.low), abs(float(target.close - target.open))
        upper_wick, lower_wick = float(target.high - max(target.open, target.close)), float(min(target.open, target.close) - target.low)
        moving_averages = {}
        for period, values in averages.items():
            value = values[-1]
            moving_averages[f"ema{period}"] = {"value": _round(value), "price_relation": None if value is None else "above" if closes[-1] > value else "below" if closes[-1] < value else "equal", "distance": None if value is None else _round(closes[-1] - value), "normalized_distance": None if value in (None, 0) else _round((closes[-1] - value) / value), "slope": None if value is None or values[-2] is None else _round(value - values[-2])}
        return {
            "schema_version": FEATURE_SCHEMA_VERSION, "instrument": target.instrument_id, "timeframe": target.timeframe, "candle_timestamp": target.start.isoformat(), "candle_state": "CLOSED" if target.is_closed else "FORMING",
            "moving_averages": moving_averages,
            "ema_ordering": {f"ema{a}_gt_ema{b}": None if averages[a][-1] is None or averages[b][-1] is None else averages[a][-1] > averages[b][-1] for a, b in ((10, 20), (20, 50), (50, 100), (100, 200))},
            "rsi14": {"value": _round(rsi_value), "state": _rsi_state(rsi_value, self.config)},
            "macd": {"line": _round(line[-1]), "signal": _round(signal[-1]), "histogram": _round(current_histogram), "polarity": None if line[-1] is None else "positive" if line[-1] > 0 else "negative" if line[-1] < 0 else "zero", "relative_to_signal": None if line[-1] is None or signal[-1] is None else "above" if line[-1] > signal[-1] else "below" if line[-1] < signal[-1] else "equal", "histogram_state": histogram_state},
            "bollinger": {"upper": _round(upper[-1]), "middle": _round(middle[-1]), "lower": _round(lower[-1]), "width": None if upper[-1] is None else _round(upper[-1] - lower[-1]), "position": _round(position), "distance_to_upper": None if upper[-1] is None else _round(upper[-1] - closes[-1]), "distance_to_lower": None if lower[-1] is None else _round(closes[-1] - lower[-1]), "zone": zone},
            "atr14": {"value": _round(atr_value), "percentage_of_price": None if atr_value is None else _round(atr_value / closes[-1] * 100), "recent_comparison": atr_comparison},
            "candle": {"open": _round(float(target.open)), "high": _round(float(target.high)), "low": _round(float(target.low)), "close": _round(float(target.close)), "body_size": _round(body), "total_range": _round(candle_range), "upper_wick": _round(upper_wick), "lower_wick": _round(lower_wick), "body_range_ratio": None if candle_range == 0 else _round(body / candle_range), "upper_wick_range_ratio": None if candle_range == 0 else _round(upper_wick / candle_range), "lower_wick_range_ratio": None if candle_range == 0 else _round(lower_wick / candle_range), "direction": "bullish" if target.close > target.open else "bearish" if target.close < target.open else "neutral", "range_vs_atr": None if atr_value in (None, 0) else _round(candle_range / atr_value)},
            "gaps": _gaps(history), "structure": structure,
            "support_resistance": {"rolling_high": _round(max(float(c.high) for c in history[-20:])), "rolling_low": _round(min(float(c.low) for c in history[-20:])), "distance_to_rolling_high": _round(max(float(c.high) for c in history[-20:]) - closes[-1]), "distance_to_rolling_low": _round(closes[-1] - min(float(c.low) for c in history[-20:])), "recent_swing_highs": structure["confirmed_highs"][-5:], "recent_swing_lows": structure["confirmed_lows"][-5:]},
            "divergence_inputs": {"price_close": _round(closes[-1]), "rsi14": _round(rsi_value), "macd_histogram": _round(current_histogram)}, "config": asdict(self.config),
        }
