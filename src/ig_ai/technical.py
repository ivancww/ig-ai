"""Deterministic, target-bounded technical feature calculations."""
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
    atr_period: int = 14
    swing_left: int = 1
    swing_right: int = 1
    touch_tolerance: float = 0.001


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 10)


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
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        raise ValueError("RSI period must be positive")
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    gain, loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    def value() -> float:
        return 100.0 if loss == 0 and gain else 50.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    result[period] = value()
    for index in range(period + 1, len(values)):
        gain = (gain * (period - 1) + gains[index - 1]) / period
        loss = (loss * (period - 1) + losses[index - 1]) / period
        result[index] = value()
    return result


def true_ranges(candles: list[Candle]) -> list[float]:
    ranges = []
    for index, candle in enumerate(candles):
        previous = float(candles[index - 1].close) if index else float(candle.close)
        ranges.append(max(float(candle.high) - float(candle.low), abs(float(candle.high) - previous), abs(float(candle.low) - previous)))
    return ranges


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    values, result = true_ranges(candles), [None] * len(candles)
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


def _structure(candles: list[Candle], config: TechnicalConfig) -> dict[str, Any]:
    highs, lows = [], []
    for index in range(config.swing_left, len(candles) - config.swing_right):
        current = candles[index]
        if float(current.high) > max(float(c.high) for c in candles[index - config.swing_left:index]) and float(current.high) >= max(float(c.high) for c in candles[index + 1:index + config.swing_right + 1]):
            highs.append({"timestamp": current.start.isoformat(), "price": _r(float(current.high)), "confirmed_at": candles[index + config.swing_right].start.isoformat()})
        if float(current.low) < min(float(c.low) for c in candles[index - config.swing_left:index]) and float(current.low) <= min(float(c.low) for c in candles[index + 1:index + config.swing_right + 1]):
            lows.append({"timestamp": current.start.isoformat(), "price": _r(float(current.low)), "confirmed_at": candles[index + config.swing_right].start.isoformat()})
    def label(points: list[dict], up: str, down: str) -> list[dict]:
        previous, result = None, []
        for point in points:
            result.append({**point, "classification": None if previous is None else up if point["price"] > previous else down})
            previous = point["price"]
        return result
    highs, lows = label(highs, "HH", "LH"), label(lows, "HL", "LL")
    candidate_high = not candles[-1].is_closed and len(candles) > config.swing_left and float(candles[-1].high) > max(float(c.high) for c in candles[-config.swing_left - 1:-1])
    candidate_low = not candles[-1].is_closed and len(candles) > config.swing_left and float(candles[-1].low) < min(float(c.low) for c in candles[-config.swing_left - 1:-1])
    return {"confirmed_highs": highs[-10:], "confirmed_lows": lows[-10:], "candidate_high": _r(float(candles[-1].high)) if candidate_high else None, "candidate_low": _r(float(candles[-1].low)) if candidate_low else None}


def _gaps(candles: list[Candle], config: TechnicalConfig) -> list[dict]:
    result = []
    for index in range(1, len(candles)):
        previous, current = candles[index - 1], candles[index]
        if float(current.low) > float(previous.high):
            direction, size = "up", float(current.low) - float(previous.high)
            filled_at = next((c.start.isoformat() for c in candles[index + 1:] if float(c.low) <= float(previous.high)), None)
        elif float(current.high) < float(previous.low):
            direction, size = "down", float(previous.low) - float(current.high)
            filled_at = next((c.start.isoformat() for c in candles[index + 1:] if float(c.high) >= float(previous.low)), None)
        else:
            continue
        result.append({"timestamp": current.start.isoformat(), "direction": direction, "size": _r(size), "percentage": _r(size / float(previous.close) * 100), "open": filled_at is None, "filled": filled_at is not None, "filled_at": filled_at})
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
        averages = {p: ema(closes, p) for p in (10, 20, 50, 100, 200)}
        def at(values: list[float | None]) -> float | None:
            return _r(values[-1])
        fast, slow = ema(closes, 12), ema(closes, 26)
        line = [None if a is None or b is None else a - b for a, b in zip(fast, slow, strict=True)]
        signal = [None] * len(line)
        first = next((i for i, value in enumerate(line) if value is not None), None)
        if first is not None:
            signal[first:] = ema([value for value in line[first:] if value is not None], 9)
        histogram = [None if a is None or b is None else a - b for a, b in zip(line, signal, strict=True)]
        period = self.config.bollinger_period
        middle, upper, lower = [None] * len(closes), [None] * len(closes), [None] * len(closes)
        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1:i + 1]
            mean = sum(window) / period
            deviation = sqrt(sum((x - mean) ** 2 for x in window) / period)
            middle[i], upper[i], lower[i] = mean, mean + self.config.bollinger_stddevs * deviation, mean - self.config.bollinger_stddevs * deviation
        rsi_values, atr_values = rsi(closes), atr(history, self.config.atr_period)
        rsi_value, atr_value = rsi_values[-1], atr_values[-1]
        structure = _structure(history, self.config)
        candle_range, body = float(target.high - target.low), abs(float(target.close - target.open))
        position = None if upper[-1] is None or upper[-1] == lower[-1] else (closes[-1] - lower[-1]) / (upper[-1] - lower[-1])
        zone = None if position is None else "above_upper" if closes[-1] > upper[-1] else "below_lower" if closes[-1] < lower[-1] else "upper_zone" if position >= .5 else "lower_zone"
        mas = {f"ema{p}": {"value": at(values), "price_relation": None if values[-1] is None else "above" if closes[-1] > values[-1] else "below" if closes[-1] < values[-1] else "equal", "distance": None if values[-1] is None else _r(closes[-1] - values[-1]), "normalized_distance": None if values[-1] in (None, 0) else _r((closes[-1] - values[-1]) / values[-1]), "slope": None if len(values) < 2 or values[-1] is None or values[-2] is None else _r(values[-1] - values[-2])} for p, values in averages.items()}
        return {"schema_version": FEATURE_SCHEMA_VERSION, "instrument": target.instrument_id, "timeframe": target.timeframe, "candle_timestamp": target.start.isoformat(), "candle_state": "CLOSED" if target.is_closed else "FORMING", "moving_averages": mas, "ema_ordering": {"ema10_gt_ema20": None if averages[10][-1] is None or averages[20][-1] is None else averages[10][-1] > averages[20][-1], "ema20_gt_ema50": None if averages[20][-1] is None or averages[50][-1] is None else averages[20][-1] > averages[50][-1], "ema50_gt_ema100": None if averages[50][-1] is None or averages[100][-1] is None else averages[50][-1] > averages[100][-1], "ema100_gt_ema200": None if averages[100][-1] is None or averages[200][-1] is None else averages[100][-1] > averages[200][-1]}, "rsi14": {"value": _r(rsi_value), "state": _rsi_state(rsi_value, self.config)}, "macd": {"line": at(line), "signal": at(signal), "histogram": at(histogram), "polarity": None if line[-1] is None else "positive" if line[-1] > 0 else "negative" if line[-1] < 0 else "zero", "relative_to_signal": None if line[-1] is None or signal[-1] is None else "above" if line[-1] > signal[-1] else "below" if line[-1] < signal[-1] else "equal"}, "bollinger": {"upper": at(upper), "middle": at(middle), "lower": at(lower), "width": None if upper[-1] is None or lower[-1] is None else _r(upper[-1] - lower[-1]), "position": _r(position), "distance_to_upper": None if upper[-1] is None else _r(upper[-1] - closes[-1]), "distance_to_lower": None if lower[-1] is None else _r(closes[-1] - lower[-1]), "zone": zone}, "atr14": {"value": _r(atr_value), "percentage_of_price": None if atr_value is None else _r(atr_value / closes[-1] * 100), "recent_comparison": None if atr_value is None else "higher_than_recent" if atr_value > sum(x for x in atr_values[:-1] if x is not None) / max(1, len([x for x in atr_values[:-1] if x is not None])) else "not_higher_than_recent"}, "candle": {"open": _r(float(target.open)), "high": _r(float(target.high)), "low": _r(float(target.low)), "close": _r(float(target.close)), "body_size": _r(body), "total_range": _r(candle_range), "upper_wick": _r(float(target.high - max(target.open, target.close))), "lower_wick": _r(float(min(target.open, target.close) - target.low)), "body_range_ratio": None if candle_range == 0 else _r(body / candle_range), "upper_wick_range_ratio": None if candle_range == 0 else _r(float(target.high - max(target.open, target.close)) / candle_range), "lower_wick_range_ratio": None if candle_range == 0 else _r(float(min(target.open, target.close) - target.low) / candle_range), "direction": "bullish" if target.close > target.open else "bearish" if target.close < target.open else "neutral", "range_vs_atr": None if atr_value in (None, 0) else _r(candle_range / atr_value)}, "gaps": _gaps(history, self.config), "structure": structure, "support_resistance": {"rolling_high": _r(max(float(c.high) for c in history[-20:])), "rolling_low": _r(min(float(c.low) for c in history[-20:])), "distance_to_rolling_high": _r(max(float(c.high) for c in history[-20:]) - closes[-1]), "distance_to_rolling_low": _r(closes[-1] - min(float(c.low) for c in history[-20:])), "recent_swing_highs": structure["confirmed_highs"][-5:], "recent_swing_lows": structure["confirmed_lows"][-5:]}, "divergence_inputs": {"price_close": _r(closes[-1]), "rsi14": _r(rsi_value), "macd_histogram": _r(histogram[-1])}, "config": asdict(self.config)}
