"""Deterministic Phase 2B pattern, structure, divergence and context engines.

All engines receive candles ordered by time and truncate at ``target_index``.
They emit evidence/state records only; no record is a trade instruction and no
probability is calculated.  A forming target may be FORMING, but confirmed
swings and divergence always use closed candles and closed right-side pivots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .direction import DirectionScoreEngine
from .models import Candle
from .technical import TechnicalFeatureEngine, _structure, ema, rsi

PATTERN_SCHEMA_VERSION = "patterns_v1"
LIFECYCLE_STATES = ("FORMING", "POTENTIAL", "NEAR_CONFIRMATION", "CONFIRMED", "FAILED")
CANDLESTICK_PATTERN_NAMES = ("Doji", "Hammer", "Inverted Hammer", "Shooting Star", "Hanging Man", "Spinning Top", "Marubozu", "Long Upper Wick", "Long Lower Wick", "Inside Bar", "Outside Bar", "Bullish Engulfing", "Bearish Engulfing", "Bullish Harami", "Bearish Harami", "Piercing Line", "Dark Cloud Cover", "Morning Star", "Evening Star", "Tweezer Top", "Tweezer Bottom", "Three White Soldiers", "Three Black Crows")
CHART_PATTERN_NAMES = ("Double Top", "Double Bottom", "Triple Top", "Triple Bottom", "Head & Shoulders", "Inverse Head & Shoulders", "Triangle", "Rising Wedge", "Falling Wedge", "Flag", "Pennant", "Rectangle", "Cup & Handle", "Rounded Top", "Rounded Bottom", "Island Reversal")


@dataclass(frozen=True)
class PatternConfig:
    doji_body_ratio: float = 0.10
    spinning_top_body_ratio: float = 0.30
    marubozu_wick_ratio: float = 0.05
    long_wick_ratio: float = 0.60
    equality_tolerance: float = 0.002
    pattern_tolerance: float = 0.03
    neckline_break_tolerance: float = 0.001


def _f(candle: Candle) -> dict[str, float]:
    candle_range = float(candle.high - candle.low)
    body = abs(float(candle.close - candle.open))
    return {
        "body": body, "range": candle_range,
        "upper": float(candle.high - max(candle.open, candle.close)),
        "lower": float(min(candle.open, candle.close) - candle.low),
    }


def _bull(candle: Candle) -> bool:
    return candle.close > candle.open


def _bear(candle: Candle) -> bool:
    return candle.close < candle.open


def _prior_trend(candles: list[Candle], index: int, direction: str, length: int = 3) -> bool:
    if index < length:
        return False
    closes = [float(c.close) for c in candles[index - length:index]]
    pairs = zip(closes, closes[1:], strict=False)
    return all(left < right for left, right in pairs) if direction == "up" else all(left > right for left, right in pairs)


def _record(name: str, candle: Candle, lifecycle: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": PATTERN_SCHEMA_VERSION, "pattern": name,
        "instance_id": f"{name}:{candle.start.isoformat()}",
        "instrument": candle.instrument_id, "timeframe": candle.timeframe,
        "start": candle.start.isoformat(), "end": candle.end.isoformat(),
        "lifecycle": lifecycle, "candle_state": "CLOSED" if candle.is_closed else "FORMING",
        "evidence": evidence or {},
    }


class CandlestickPatternEngine:
    """Recognise objective single- and multi-candle geometries."""

    def __init__(self, config: PatternConfig | None = None):
        self.config = config or PatternConfig()

    @staticmethod
    def supported_patterns() -> tuple[str, ...]:
        return CANDLESTICK_PATTERN_NAMES

    def detect(self, candles: list[Candle], *, target_index: int | None = None) -> list[dict[str, Any]]:
        if not candles:
            return []
        index = len(candles) - 1 if target_index is None else target_index
        if not 0 <= index < len(candles):
            raise IndexError("target_index outside candle history")
        target = candles[index]
        current = _f(target)
        result: list[dict[str, Any]] = []
        lifecycle = "CONFIRMED" if target.is_closed else "FORMING"
        ratio = current["body"] / current["range"] if current["range"] else 0.0

        def add(name: str, **evidence: Any) -> None:
            result.append(_record(name, target, lifecycle, evidence))

        if ratio <= self.config.doji_body_ratio:
            add("Doji", body_range_ratio=ratio)
        if self.config.doji_body_ratio < ratio <= self.config.spinning_top_body_ratio:
            add("Spinning Top", body_range_ratio=ratio)
        if current["range"] and current["upper"] / current["range"] >= self.config.long_wick_ratio:
            add("Long Upper Wick", wick_ratio=current["upper"] / current["range"])
        if current["range"] and current["lower"] / current["range"] >= self.config.long_wick_ratio:
            add("Long Lower Wick", wick_ratio=current["lower"] / current["range"])
        if current["range"] and current["upper"] / current["range"] <= self.config.marubozu_wick_ratio and current["lower"] / current["range"] <= self.config.marubozu_wick_ratio:
            add("Marubozu", direction="bullish" if _bull(target) else "bearish" if _bear(target) else "neutral")
        if current["body"] and current["lower"] >= 2 * current["body"] and current["upper"] <= current["body"] * 0.5:
            if _prior_trend(candles, index, "down"):
                add("Hammer", prior_trend="down")
            if _prior_trend(candles, index, "up"):
                add("Hanging Man", prior_trend="up")
        if current["body"] and current["upper"] >= 2 * current["body"] and current["lower"] <= current["body"] * 0.5:
            if _prior_trend(candles, index, "down"):
                add("Inverted Hammer", prior_trend="down")
            if _prior_trend(candles, index, "up"):
                add("Shooting Star", prior_trend="up")

        if index >= 1:
            previous = candles[index - 1]
            if float(target.high) <= float(previous.high) and float(target.low) >= float(previous.low):
                add("Inside Bar")
            if float(target.high) >= float(previous.high) and float(target.low) <= float(previous.low):
                add("Outside Bar")
            if _bull(target) and _bear(previous) and target.open <= previous.close and target.close >= previous.open:
                add("Bullish Engulfing")
            if _bear(target) and _bull(previous) and target.open >= previous.close and target.close <= previous.open:
                add("Bearish Engulfing")
            if _bear(previous) and _bull(target) and target.open >= previous.close and target.close <= previous.open:
                add("Bullish Harami")
            if _bull(previous) and _bear(target) and target.open <= previous.close and target.close >= previous.open:
                add("Bearish Harami")
            if _bear(previous) and _bull(target) and target.open < previous.low and target.close > (previous.open + previous.close) / 2 and target.close < previous.open:
                add("Piercing Line")
            if _bull(previous) and _bear(target) and target.open > previous.high and target.close < (previous.open + previous.close) / 2 and target.close > previous.open:
                add("Dark Cloud Cover")
            if abs(float(target.low) - float(previous.low)) <= self.config.equality_tolerance * max(float(target.close), 1.0) and _bear(previous) and _bull(target):
                add("Tweezer Bottom")
            if abs(float(target.high) - float(previous.high)) <= self.config.equality_tolerance * max(float(target.close), 1.0) and _bull(previous) and _bear(target):
                add("Tweezer Top")
        if index >= 2:
            a, b, c = candles[index - 2:index + 1]
            fa, fb = _f(a), _f(b)
            if _bear(a) and fb["body"] <= fa["body"] * 0.6 and _bull(c) and c.close > (a.open + a.close) / 2:
                add("Morning Star")
            if _bull(a) and fb["body"] <= fa["body"] * 0.6 and _bear(c) and c.close < (a.open + a.close) / 2:
                add("Evening Star")
            if _bull(a) and _bull(b) and _bull(c) and a.close < b.close < c.close and all(_f(x)["upper"] <= _f(x)["body"] * 0.25 for x in (a, b, c)):
                add("Three White Soldiers")
            if _bear(a) and _bear(b) and _bear(c) and a.close > b.close > c.close and all(_f(x)["lower"] <= _f(x)["body"] * 0.25 for x in (a, b, c)):
                add("Three Black Crows")
        return result


def _oscillator_series(candles: list[Candle]) -> tuple[list[float | None], list[float | None]]:
    closes = [float(c.close) for c in candles]
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [None if a is None or b is None else a - b for a, b in zip(fast, slow, strict=True)]
    signal = ema([x for x in line if x is not None], 9)
    first = next((i for i, x in enumerate(line) if x is not None), len(line))
    full_signal: list[float | None] = [None] * len(line)
    full_signal[first:] = signal
    return rsi(closes), [None if a is None or b is None else a - b for a, b in zip(line, full_signal, strict=True)]


class DivergenceEngine:
    def detect(self, candles: list[Candle], *, target_index: int | None = None) -> list[dict[str, Any]]:
        if not candles:
            return []
        index = len(candles) - 1 if target_index is None else target_index
        history = candles[:index + 1]
        structure = _structure(history, TechnicalFeatureEngine().config)
        rsi_values, macd_values = _oscillator_series(history)
        output = []
        for kind, points, oscillator, bullish_name, bearish_name in (("low", structure["confirmed_lows"], rsi_values, "Bullish RSI Divergence", ""), ("low", structure["confirmed_lows"], macd_values, "Bullish MACD Divergence", ""), ("high", structure["confirmed_highs"], rsi_values, "", "Bearish RSI Divergence"), ("high", structure["confirmed_highs"], macd_values, "", "Bearish MACD Divergence")):
            if len(points) < 2:
                continue
            left, right = points[-2:]
            left_index = next((i for i, c in enumerate(history) if c.start.isoformat() == left["timestamp"]), None)
            right_index = next((i for i, c in enumerate(history) if c.start.isoformat() == right["timestamp"]), None)
            if left_index is None or right_index is None or oscillator[left_index] is None or oscillator[right_index] is None:
                continue
            bullish = kind == "low" and float(right["price"]) < float(left["price"]) and oscillator[right_index] > oscillator[left_index]
            bearish = kind == "high" and float(right["price"]) > float(left["price"]) and oscillator[right_index] < oscillator[left_index]
            if bullish or bearish:
                output.append({"pattern": bullish_name or bearish_name, "lifecycle": "CONFIRMED", "instrument": history[-1].instrument_id, "timeframe": history[-1].timeframe, "start": left["timestamp"], "end": right["timestamp"], "evidence": {"price_pivots": [left, right], "oscillator_values": [oscillator[left_index], oscillator[right_index]], "confirmed_only": True}})
        return output


class MarketStructureEngine:
    def analyze(self, candles: list[Candle], *, target_index: int | None = None) -> dict[str, Any]:
        if not candles:
            return {}
        index = len(candles) - 1 if target_index is None else target_index
        history, target = candles[:index + 1], candles[index]
        structure = _structure(history, TechnicalFeatureEngine().config)
        highs, lows = structure["confirmed_highs"], structure["confirmed_lows"]
        close = float(target.close)
        last_high = highs[-1] if highs else None
        last_low = lows[-1] if lows else None
        breakout = "up" if last_high and close > float(last_high["price"]) else "down" if last_low and close < float(last_low["price"]) else None
        previous_close = float(history[-2].close) if len(history) > 1 else close
        retest = bool((last_high and previous_close > float(last_high["price"]) and float(target.low) <= float(last_high["price"]) <= close) or (last_low and previous_close < float(last_low["price"]) and float(target.high) >= float(last_low["price"]) >= close))
        false_breakout_up = bool(last_high and float(target.high) > float(last_high["price"]) and close <= float(last_high["price"]))
        false_breakout_down = bool(last_low and float(target.low) < float(last_low["price"]) and close >= float(last_low["price"]))
        false_breakout = false_breakout_up or false_breakout_down
        labels = [p["classification"] for p in highs[-2:] + lows[-2:] if p.get("classification")]
        direction = "UP_STRUCTURE" if "HH" in labels and "HL" in labels else "DOWN_STRUCTURE" if "LH" in labels and "LL" in labels else "RANGE"
        prior_labels = [p["classification"] for p in highs[-3:-1] + lows[-3:-1] if p.get("classification")]
        prior_direction = "UP" if "HH" in prior_labels and "HL" in prior_labels else "DOWN" if "LH" in prior_labels and "LL" in prior_labels else "NEUTRAL"
        breakout_direction = breakout
        current_direction = "UP" if direction == "UP_STRUCTURE" else "DOWN" if direction == "DOWN_STRUCTURE" else "NEUTRAL"
        break_of_structure = bool(breakout_direction and prior_direction in {"UP", "DOWN"} and breakout_direction != prior_direction)
        ranges = [float(c.high - c.low) for c in history[-5:]]
        compression = len(ranges) >= 3 and ranges[-1] < sum(ranges[:-1]) / len(ranges[:-1])
        expansion = len(ranges) >= 3 and ranges[-1] > sum(ranges[:-1]) / len(ranges[:-1])
        return {"schema_version": PATTERN_SCHEMA_VERSION, "instrument": target.instrument_id, "timeframe": target.timeframe, "candle_timestamp": target.start.isoformat(), "candle_state": "CLOSED" if target.is_closed else "FORMING", "direction": direction, "current_direction": current_direction, "prior_direction": prior_direction, "confirmed_highs": highs[-5:], "confirmed_lows": lows[-5:], "break_of_structure": break_of_structure, "breakout": breakout, "breakout_direction": breakout_direction, "retest": bool(retest), "false_breakout": false_breakout, "false_breakout_direction": "UP" if false_breakout_up else "DOWN" if false_breakout_down else None, "compression": compression, "expansion": expansion}


class ChartPatternEngine:
    """Conservative geometric foundations; absent evidence means no pattern."""

    def detect(self, candles: list[Candle], *, target_index: int | None = None) -> list[dict[str, Any]]:
        if not candles:
            return []
        index = len(candles) - 1 if target_index is None else target_index
        history, target = candles[:index + 1], candles[index]
        structure = _structure(history, TechnicalFeatureEngine().config)
        highs, lows = structure["confirmed_highs"], structure["confirmed_lows"]
        output: list[dict[str, Any]] = []
        tolerance = 0.03

        def emit(name: str, points: list[dict[str, Any]], level: float | None = None, lifecycle: str = "POTENTIAL", invalidation: float | None = None) -> None:
            output.append({"schema_version": PATTERN_SCHEMA_VERSION, "pattern": name, "instance_id": f"{name}:{points[0]['timestamp']}", "instrument": target.instrument_id, "timeframe": target.timeframe, "start": points[0]["timestamp"], "end": target.end.isoformat(), "swing_points": points, "lifecycle": lifecycle if target.is_closed else "FORMING", "confirmation_level": level, "invalidation_level": invalidation, "context": {"candle_state": "CLOSED" if target.is_closed else "FORMING"}})

        if len(highs) >= 2:
            pair = highs[-2:]
            level = min((float(p["price"]) for p in lows if pair[0]["timestamp"] < p["timestamp"] < pair[1]["timestamp"]), default=None)
            if abs(float(pair[0]["price"]) - float(pair[1]["price"])) <= tolerance * max(float(pair[0]["price"]), 1):
                lifecycle = "FAILED" if float(target.close) > max(float(p["price"]) for p in pair) else "CONFIRMED" if level is not None and float(target.close) < level else "NEAR_CONFIRMATION" if level is not None else "POTENTIAL"
                emit("Double Top", pair, level, lifecycle, max(float(p["price"]) for p in pair))
        if len(lows) >= 2:
            pair = lows[-2:]
            level = max((float(p["price"]) for p in highs if pair[0]["timestamp"] < p["timestamp"] < pair[1]["timestamp"]), default=None)
            if abs(float(pair[0]["price"]) - float(pair[1]["price"])) <= tolerance * max(float(pair[0]["price"]), 1):
                lifecycle = "FAILED" if float(target.close) < min(float(p["price"]) for p in pair) else "CONFIRMED" if level is not None and float(target.close) > level else "NEAR_CONFIRMATION" if level is not None else "POTENTIAL"
                emit("Double Bottom", pair, level, lifecycle, min(float(p["price"]) for p in pair))
        if len(highs) >= 3:
            trio = highs[-3:]
            level = min((float(p["price"]) for p in lows if trio[0]["timestamp"] < p["timestamp"] < trio[2]["timestamp"]), default=None)
            if max(float(p["price"]) for p in trio) - min(float(p["price"]) for p in trio) <= tolerance * max(float(trio[0]["price"]), 1):
                lifecycle = "FAILED" if float(target.close) > max(float(p["price"]) for p in trio) else "CONFIRMED" if level and float(target.close) < level else "POTENTIAL"
                emit("Triple Top", trio, level, lifecycle, max(float(p["price"]) for p in trio))
        if len(lows) >= 3:
            trio = lows[-3:]
            level = max((float(p["price"]) for p in highs if trio[0]["timestamp"] < p["timestamp"] < trio[2]["timestamp"]), default=None)
            if max(float(p["price"]) for p in trio) - min(float(p["price"]) for p in trio) <= tolerance * max(float(trio[0]["price"]), 1):
                lifecycle = "FAILED" if float(target.close) < min(float(p["price"]) for p in trio) else "CONFIRMED" if level and float(target.close) > level else "POTENTIAL"
                emit("Triple Bottom", trio, level, lifecycle, min(float(p["price"]) for p in trio))
        if len(highs) >= 3:
            trio = highs[-3:]
            if float(trio[1]["price"]) > float(trio[0]["price"]) and float(trio[1]["price"]) > float(trio[2]["price"]):
                neckline = min((float(p["price"]) for p in lows if trio[0]["timestamp"] < p["timestamp"] < trio[2]["timestamp"]), default=None)
                lifecycle = "FAILED" if float(target.close) > max(float(p["price"]) for p in (trio[0], trio[2])) else "CONFIRMED" if neckline and float(target.close) < neckline else "POTENTIAL"
                emit("Head & Shoulders", trio, neckline, lifecycle, max(float(p["price"]) for p in (trio[0], trio[2])))
        if len(lows) >= 3:
            trio = lows[-3:]
            if float(trio[1]["price"]) < float(trio[0]["price"]) and float(trio[1]["price"]) < float(trio[2]["price"]):
                neckline = max((float(p["price"]) for p in highs if trio[0]["timestamp"] < p["timestamp"] < trio[2]["timestamp"]), default=None)
                lifecycle = "FAILED" if float(target.close) < min(float(p["price"]) for p in (trio[0], trio[2])) else "CONFIRMED" if neckline and float(target.close) > neckline else "POTENTIAL"
                emit("Inverse Head & Shoulders", trio, neckline, lifecycle, min(float(p["price"]) for p in (trio[0], trio[2])))
        if len(highs) >= 3 and len(lows) >= 3:
            high_values = [float(p["price"]) for p in highs[-3:]]
            low_values = [float(p["price"]) for p in lows[-3:]]
            if high_values[-1] < high_values[0] and low_values[-1] > low_values[0]:
                emit("Triangle", highs[-3:] + lows[-3:])
            elif high_values[-1] > high_values[0] and low_values[-1] > low_values[0]:
                emit("Rising Wedge", highs[-3:] + lows[-3:])
            elif high_values[-1] < high_values[0] and low_values[-1] < low_values[0]:
                high_slope = high_values[-1] - high_values[0]
                low_slope = low_values[-1] - low_values[0]
                emit("Flag" if abs(high_slope - low_slope) <= tolerance * max(high_values) else "Falling Wedge", highs[-3:] + lows[-3:])
            elif max(high_values) - min(high_values) <= tolerance * max(high_values) and max(low_values) - min(low_values) <= tolerance * max(low_values):
                emit("Rectangle", highs[-3:] + lows[-3:])
            elif high_values[0] > high_values[1] < high_values[2] and low_values[0] > low_values[1] < low_values[2]:
                emit("Pennant", highs[-3:] + lows[-3:])
        if len(lows) >= 3:
            low_values = [float(p["price"]) for p in lows[-3:]]
            if low_values[1] < low_values[0] and low_values[1] < low_values[2]:
                emit("Cup & Handle", lows[-3:])
            if low_values[1] > low_values[0] and low_values[1] > low_values[2]:
                emit("Rounded Bottom", lows[-3:])
        if len(highs) >= 3:
            high_values = [float(p["price"]) for p in highs[-3:]]
            if high_values[1] < high_values[0] and high_values[1] < high_values[2]:
                emit("Rounded Top", highs[-3:])
        if len(history) >= 3:
            first_up = float(history[-2].low) > float(history[-3].high)
            first_down = float(history[-2].high) < float(history[-3].low)
            second_up = float(target.low) > float(history[-2].high)
            second_down = float(target.high) < float(history[-2].low)
            if target.is_closed and ((first_up and second_down) or (first_down and second_up)):
                emit("Island Reversal", [{"timestamp": history[-2].start.isoformat(), "price": float(history[-2].close)}], None, "POTENTIAL")
        return output

    @staticmethod
    def supported_patterns() -> tuple[str, ...]:
        return CHART_PATTERN_NAMES


def lifecycle_transition(previous: str, *, forming: bool = False, confirmed: bool = False, invalidated: bool = False) -> str:
    """Apply the shared lifecycle without inventing a transition from data."""
    if invalidated:
        return "FAILED"
    if forming:
        return "FORMING"
    if confirmed:
        return "CONFIRMED"
    if previous == "FORMING":
        return "POTENTIAL"
    if previous == "POTENTIAL":
        return "NEAR_CONFIRMATION"
    return previous if previous in LIFECYCLE_STATES else "POTENTIAL"


class DirectionReversalEngine:
    """Combine deterministic timeframe evidence without producing signals."""

    def classify(self, features_by_timeframe: dict[str, dict[str, Any]], divergences_by_timeframe: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        divergences_by_timeframe = divergences_by_timeframe or {}
        primary = features_by_timeframe.get("1H", {})
        structure = primary.get("structure", {})
        highs = structure.get("confirmed_highs", [])
        lows = structure.get("confirmed_lows", [])
        labels = [point.get("classification") for point in highs[-2:] + lows[-2:]]
        one_hour_trend = "UP_STRUCTURE" if "HH" in labels and "HL" in labels else "DOWN_STRUCTURE" if "LH" in labels and "LL" in labels else "RANGE"
        higher_context = {}
        for timeframe in ("4H", "1D"):
            context_structure = features_by_timeframe.get(timeframe, {}).get("structure", {})
            context_labels = [point.get("classification") for point in context_structure.get("confirmed_highs", [])[-2:] + context_structure.get("confirmed_lows", [])[-2:]]
            higher_context[timeframe] = "UP_STRUCTURE" if "HH" in context_labels and "HL" in context_labels else "DOWN_STRUCTURE" if "LH" in context_labels and "LL" in context_labels else "RANGE"
        evidence = []
        if divergences_by_timeframe.get("15M") and one_hour_trend in {"UP_STRUCTURE", "DOWN_STRUCTURE"}:
            evidence.append("15M_DIVERGENCE_EARLY_WARNING")
        macd = primary.get("macd", {})
        if macd.get("histogram_state") == "weakening":
            evidence.append("1H_MOMENTUM_WEAKENING")
        primary_structure = primary.get("structure", {})
        if "LH" in labels and "LL" in labels:
            state = "REVERSAL_CONFIRMED"
        elif primary_structure.get("break_of_structure"):
            state = "REVERSAL_WATCH"
        elif "15M_DIVERGENCE_EARLY_WARNING" in evidence and "1H_MOMENTUM_WEAKENING" in evidence:
            state = "EXHAUSTION_RISK"
        elif "15M_DIVERGENCE_EARLY_WARNING" in evidence:
            state = "EARLY"
        elif "1H_MOMENTUM_WEAKENING" in evidence:
            state = "MATURE"
        else:
            state = "CONFIRMED" if one_hour_trend != "RANGE" else "EARLY"
        return {"schema_version": PATTERN_SCHEMA_VERSION, "primary_timeframe": "1H", "timeframe_roles": {"15M": "EARLY_WARNING", "1H": "PRIMARY_DIRECTION", "4H": "HIGHER_CONTEXT", "1D": "BROADER_CONTEXT"}, "direction_structure": one_hour_trend, "higher_timeframe_context": higher_context, "state": state, "evidence": evidence, "probability": None, "recommendation": None}


class MultiTimeframeCoordinator:
    """Coordinate distinct target-bounded histories without mixing candles."""

    def __init__(self, engine: Phase2BEngine | None = None, max_history: int = 600):
        self.engine = engine or Phase2BEngine()
        self.max_history = max_history

    def analyze(self, histories: dict[str, list[Candle]], *, target_time: datetime | None = None, target_timeframe: str | None = None) -> dict[str, Any]:
        aligned: dict[str, list[Candle]] = {}
        for timeframe, candles in histories.items():
            if target_time is None:
                available = candles
            elif timeframe == target_timeframe:
                available = [c for c in candles if c.start <= target_time]
            else:
                # A higher-timeframe candle is usable only after its closed
                # end, never merely because its start precedes the target.
                available = [c for c in candles if c.end <= target_time and c.is_closed]
            aligned[timeframe] = available[-self.max_history:]
        bounded = {timeframe: candles for timeframe, candles in aligned.items() if candles}
        analyses = {timeframe: self.engine.analyze(candles) for timeframe, candles in bounded.items()}
        features = {}
        divergences = {}
        for timeframe, analysis in analyses.items():
            technical = analysis["context"]
            features[timeframe] = {"structure": analysis["structure"], "macd": technical["macd"], "rsi14": technical["rsi"]}
            divergences[timeframe] = analysis["divergences"]
        direction = DirectionReversalEngine().classify(features, divergences)
        direction_score = DirectionScoreEngine().analyze(analyses)
        return {"schema_version": PATTERN_SCHEMA_VERSION, "timeframes": analyses, "direction_reversal": direction, "direction_score": direction_score, "history_lengths": {timeframe: len(candles) for timeframe, candles in aligned.items()}}


class Phase2BEngine:
    def __init__(self, technical: TechnicalFeatureEngine | None = None):
        self.technical = technical or TechnicalFeatureEngine()
        self.candles = CandlestickPatternEngine()
        self.structure = MarketStructureEngine()
        self.chart = ChartPatternEngine()
        self.divergence = DivergenceEngine()

    def analyze(self, candles: list[Candle], *, target_index: int | None = None) -> dict[str, Any]:
        if not candles:
            raise ValueError("at least one candle is required")
        index = len(candles) - 1 if target_index is None else target_index
        history = candles[:index + 1]
        technical = self.technical.calculate(history)
        divergences = self.divergence.detect(history)
        context = {"rsi": technical["rsi14"], "macd": technical["macd"], "atr": technical["atr14"], "gaps": technical["gaps"], "support_resistance": technical["support_resistance"]}
        candlestick = self.candles.detect(history)
        chart = self.chart.detect(history)
        for observation in candlestick + chart + divergences:
            observation["context"] = {**observation.get("context", {}), **context}
        direction = DirectionReversalEngine().classify({"1H": {**technical, "structure": self.structure.analyze(history)}}) if history[-1].timeframe == "1H" else None
        return {"schema_version": PATTERN_SCHEMA_VERSION, "instrument": history[-1].instrument_id, "timeframe": history[-1].timeframe, "candle_timestamp": history[-1].start.isoformat(), "candle_state": "CLOSED" if history[-1].is_closed else "FORMING", "candlestick_patterns": candlestick, "chart_patterns": chart, "structure": self.structure.analyze(history), "divergences": divergences, "direction_reversal": direction, "context": context}
