"""Deterministic technical direction and reversal evidence.

The output is a technical score and evidence classification, never a
probability or trading instruction.  Inputs are already target-bounded by the
Phase 2B multi-timeframe coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIRECTION_SCHEMA_VERSION = "direction_score_v1"
TIMEFRAMES = ("15M", "1H", "4H", "1D")
STAGES = ("INSUFFICIENT_EVIDENCE", "EARLY", "CONFIRMED", "MATURE", "EXHAUSTION_RISK", "REVERSAL_WATCH", "REVERSAL_CONFIRMED")
RISK_LEVELS = ("LOW", "MODERATE", "HIGH", "VERY_HIGH")


@dataclass(frozen=True)
class DirectionWeights:
    version: str = DIRECTION_SCHEMA_VERSION
    primary_structure: float = 25.0
    higher_structure: float = 15.0
    broader_structure: float = 10.0
    early_confirmation: float = 10.0
    ema_structure: float = 10.0
    macd: float = 8.0
    rsi: float = 5.0
    patterns: float = 5.0
    divergence: float = 5.0
    support_breakout: float = 4.0
    volatility: float = 3.0
    forming_discount: float = 0.5

    def as_dict(self) -> dict[str, float | str]:
        return {key: value for key, value in self.__dict__.items()}


@dataclass
class DirectionScoreEngine:
    weights: DirectionWeights = field(default_factory=DirectionWeights)

    @staticmethod
    def _trend(structure: dict[str, Any] | None) -> str:
        structure = structure or {}
        direction = structure.get("direction")
        if direction in {"UP_STRUCTURE", "DOWN_STRUCTURE"}:
            return "UP" if direction == "UP_STRUCTURE" else "DOWN"
        points = structure.get("confirmed_highs", [])[-2:] + structure.get("confirmed_lows", [])[-2:]
        labels = {point.get("classification") for point in points}
        if {"HH", "HL"} <= labels:
            return "UP"
        if {"LH", "LL"} <= labels:
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _add(ledger: list[dict[str, Any]], *, evidence_type: str, timeframe: str, direction: str, weight: float, raw_state: Any, timestamp: str | None, confirmed: bool, forming_discount: float) -> None:
        if not weight:
            return
        applied = weight if confirmed else weight * forming_discount
        if direction not in {"UP", "DOWN"}:
            applied = 0.0
        ledger.append({"evidence_type": evidence_type, "timeframe": timeframe, "direction": direction, "weight": round(weight, 4), "applied_weight": round(applied, 4), "raw_state": raw_state, "timestamp": timestamp, "confirmed": confirmed, "provisional": not confirmed})

    def _score_component(self, ledger: list[dict[str, Any]], evidence_type: str, timeframe: str, state: str, weight: float, timestamp: str | None, confirmed: bool) -> None:
        self._add(ledger, evidence_type=evidence_type, timeframe=timeframe, direction=state, weight=weight, raw_state=state, timestamp=timestamp, confirmed=confirmed, forming_discount=self.weights.forming_discount)

    def _patterns_direction(self, analysis: dict[str, Any]) -> str:
        up = down = 0
        for pattern in analysis.get("candlestick_patterns", []) + analysis.get("chart_patterns", []):
            name = str(pattern.get("pattern", ""))
            if any(token in name for token in ("Bullish", "Bottom", "Morning", "Three White", "Hammer", "Piercing")):
                up += 1
            if any(token in name for token in ("Bearish", "Top", "Evening", "Three Black", "Shooting", "Hanging", "Dark Cloud")):
                down += 1
        return "UP" if up > down else "DOWN" if down > up else "NEUTRAL"

    def analyze(self, timeframes: dict[str, dict[str, Any]], *, timestamp: str | None = None) -> dict[str, Any]:
        primary = timeframes.get("1H", {})
        primary_structure = primary.get("structure", {})
        primary_trend = self._trend(primary_structure)
        ledger: list[dict[str, Any]] = []
        weights = self.weights

        for timeframe, weight, label in (("1H", weights.primary_structure, "1H_STRUCTURE"), ("4H", weights.higher_structure, "4H_CONTEXT"), ("1D", weights.broader_structure, "1D_CONTEXT"), ("15M", weights.early_confirmation, "15M_CONFIRMATION")):
            analysis = timeframes.get(timeframe, {})
            state = self._trend(analysis.get("structure"))
            self._score_component(ledger, label, timeframe, state, weight, timestamp or analysis.get("candle_timestamp"), analysis.get("candle_state") == "CLOSED")

        technical = primary.get("context", {})
        ma = technical.get("moving_averages", {})
        ordering = technical.get("ema_ordering", {})
        ema_up = sum(value is True for value in ordering.values()) >= 3
        ema_down = sum(value is False for value in ordering.values()) >= 3
        if not ema_up and not ema_down:
            relations = [ma.get(name, {}).get("price_relation") for name in ("ema20", "ema50")]
            ema_up, ema_down = relations == ["above", "above"], relations == ["below", "below"]
        self._score_component(ledger, "EMA_STRUCTURE", "1H", "UP" if ema_up else "DOWN" if ema_down else "NEUTRAL", weights.ema_structure, timestamp, primary.get("candle_state") == "CLOSED")

        macd = technical.get("macd", {})
        macd_state = "UP" if macd.get("polarity") == "positive" and macd.get("relative_to_signal") == "above" else "DOWN" if macd.get("polarity") == "negative" and macd.get("relative_to_signal") == "below" else "NEUTRAL"
        self._score_component(ledger, "MACD", "1H", macd_state, weights.macd, timestamp, primary.get("candle_state") == "CLOSED")

        rsi_value = technical.get("rsi14", {}).get("value")
        rsi_state = "UP" if rsi_value is not None and rsi_value > 50 else "DOWN" if rsi_value is not None and rsi_value < 50 else "NEUTRAL"
        self._score_component(ledger, "RSI", "1H", rsi_state, weights.rsi, timestamp, primary.get("candle_state") == "CLOSED")

        pattern_state = self._patterns_direction(primary)
        self._score_component(ledger, "PATTERNS", "1H", pattern_state, weights.patterns, timestamp, primary.get("candle_state") == "CLOSED")

        divergence_state = "NEUTRAL"
        divergences = primary.get("divergences", [])
        if not divergences:
            divergences = timeframes.get("15M", {}).get("divergences", [])
        if any("Bullish" in item.get("pattern", "") for item in divergences):
            divergence_state = "UP"
        elif any("Bearish" in item.get("pattern", "") for item in divergences):
            divergence_state = "DOWN"
        self._score_component(ledger, "DIVERGENCE", "15M" if primary.get("divergences", []) == [] else "1H", divergence_state, weights.divergence, timestamp, all(item.get("lifecycle") == "CONFIRMED" for item in divergences))

        structure = primary.get("structure", {})
        breakout = structure.get("breakout")
        sr_state = "UP" if breakout == "up" and not structure.get("false_breakout") else "DOWN" if breakout == "down" and not structure.get("false_breakout") else "NEUTRAL"
        self._score_component(ledger, "SUPPORT_RESISTANCE_BREAKOUT", "1H", sr_state, weights.support_breakout, timestamp, primary.get("candle_state") == "CLOSED")

        volatility = technical.get("atr14", {})
        bollinger = technical.get("bollinger", {})
        ledger.extend([
            {"evidence_type": "ATR_VOLATILITY", "timeframe": "1H", "direction": "NEUTRAL", "weight": weights.volatility, "applied_weight": 0.0, "raw_state": volatility.get("recent_comparison"), "timestamp": timestamp or primary.get("candle_timestamp"), "confirmed": primary.get("candle_state") == "CLOSED", "provisional": primary.get("candle_state") != "CLOSED"},
            {"evidence_type": "BOLLINGER_CONTEXT", "timeframe": "1H", "direction": "NEUTRAL", "weight": 0.0, "applied_weight": 0.0, "raw_state": bollinger.get("zone"), "timestamp": timestamp or primary.get("candle_timestamp"), "confirmed": primary.get("candle_state") == "CLOSED", "provisional": primary.get("candle_state") != "CLOSED"},
        ])
        up = sum(item["applied_weight"] for item in ledger if item["direction"] == "UP")
        down = sum(item["applied_weight"] for item in ledger if item["direction"] == "DOWN")
        total_weight = sum((weights.primary_structure, weights.higher_structure, weights.broader_structure, weights.early_confirmation, weights.ema_structure, weights.macd, weights.rsi, weights.patterns, weights.divergence, weights.support_breakout))
        confirmed_weight = sum(item["applied_weight"] for item in ledger if item["confirmed"] and item["direction"] in {"UP", "DOWN"})
        forming_weight = sum(item["applied_weight"] for item in ledger if item["provisional"] and item["direction"] in {"UP", "DOWN"})
        directional_weight = up + down
        up_score = round(100 * up / total_weight, 2) if total_weight else 0.0
        down_score = round(100 * down / total_weight, 2) if total_weight else 0.0
        coverage = {"available_weight": round(directional_weight, 4), "total_weight": round(total_weight, 4), "ratio": round(directional_weight / total_weight, 4) if total_weight else 0.0, "confirmed_weight": round(confirmed_weight, 4), "forming_weight": round(forming_weight, 4)}
        direction = primary_trend if primary_trend != "NEUTRAL" else "UP" if up_score > down_score else "DOWN" if down_score > up_score else "NEUTRAL"

        warning = self._warning_evidence(timeframes, primary_trend)
        agreement = self._agreement(timeframes, primary_trend)
        closed_primary = primary.get("candle_state") == "CLOSED"
        current_direction = primary_structure.get("current_direction") or primary_trend
        prior_direction = self._prior_direction(primary_structure)
        reversal = prior_direction in {"UP", "DOWN"} and current_direction in {"UP", "DOWN"} and prior_direction != current_direction
        bos_against_prior = primary_structure.get("break_of_structure") and primary_structure.get("breakout_direction") in {"UP", "DOWN"} and prior_direction in {"UP", "DOWN"} and primary_structure.get("breakout_direction") != prior_direction
        if directional_weight == 0:
            stage = "INSUFFICIENT_EVIDENCE"
            risk = "UNKNOWN"
            risk_score = None
            holding = "UNKNOWN"
        elif closed_primary and reversal:
            stage = "REVERSAL_CONFIRMED"
        elif closed_primary and bos_against_prior:
            stage = "REVERSAL_WATCH"
        elif warning["strong"]:
            stage = "EXHAUSTION_RISK"
        elif warning["moderate"]:
            stage = "MATURE"
        elif primary_trend != "NEUTRAL":
            stage = "CONFIRMED"
        else:
            stage = "EARLY"

        if directional_weight != 0:
            risk_score = min(100, warning["score"] + (20 if warning["strong"] else 0) + (35 if stage == "REVERSAL_WATCH" else 60 if stage == "REVERSAL_CONFIRMED" else 0))
            risk = "VERY_HIGH" if risk_score >= 75 else "HIGH" if risk_score >= 50 else "MODERATE" if risk_score >= 25 else "LOW"
            holding = self._holding_window(stage, risk, agreement, technical.get("atr14", {}))
        reference_timestamp = primary.get("candle_timestamp")
        return {"schema_version": DIRECTION_SCHEMA_VERSION, "score_version": self.weights.version, "instrument": primary.get("instrument"), "timeframe": "1H", "candle_timestamp": reference_timestamp, "model_reference": {"timeframe": "1H", "candle_timestamp": reference_timestamp, "candle_state": primary.get("candle_state", "FORMING")}, "candle_state": primary.get("candle_state", "FORMING"), "direction": direction, "up_score": up_score, "down_score": down_score, "coverage": coverage, "trend_stage": stage, "holding_window": holding, "reversal_risk": {"category": risk, "score": risk_score, "evidence": warning["items"]}, "timeframe_agreement": agreement, "evidence_ledger": ledger, "technical_score_only": True, "calibrated_probability": None, "recommendation": None}

    def _prior_direction(self, structure: dict[str, Any]) -> str:
        explicit = structure.get("prior_direction")
        if explicit in {"UP", "DOWN"}:
            return explicit
        prior_points = structure.get("confirmed_highs", [])[-3:-1] + structure.get("confirmed_lows", [])[-3:-1]
        labels = {point.get("classification") for point in prior_points}
        if {"HH", "HL"} <= labels:
            return "UP"
        if {"LH", "LL"} <= labels:
            return "DOWN"
        return "NEUTRAL"

    def _warning_evidence(self, timeframes: dict[str, dict[str, Any]], primary_trend: str) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        warning_score = 0
        early = timeframes.get("15M", {})
        opposite = "DOWN" if primary_trend == "UP" else "UP" if primary_trend == "DOWN" else "NEUTRAL"
        if primary_trend in {"UP", "DOWN"} and self._trend(early.get("structure")) == opposite:
            items.append({"type": "15M_STRUCTURE_OPPOSITE", "timeframe": "15M", "confirmed": early.get("candle_state") == "CLOSED"})
            warning_score += 20 if early.get("candle_state") == "CLOSED" else 10
        if any(("Bearish" if primary_trend == "UP" else "Bullish") in item.get("pattern", "") for item in early.get("divergences", [])):
            items.append({"type": "15M_DIVERGENCE", "timeframe": "15M", "confirmed": all(item.get("lifecycle") == "CONFIRMED" for item in early.get("divergences", []))})
            warning_score += 20 if early.get("candle_state") == "CLOSED" else 10
        macd = timeframes.get("1H", {}).get("context", {}).get("macd", {})
        macd_aligned = (primary_trend == "UP" and macd.get("polarity") == "positive") or (primary_trend == "DOWN" and macd.get("polarity") == "negative")
        if macd.get("histogram_state") == "weakening" and macd_aligned:
            items.append({"type": "1H_MACD_WEAKENING", "timeframe": "1H", "confirmed": timeframes.get("1H", {}).get("candle_state") == "CLOSED"})
            warning_score += 15 if timeframes.get("1H", {}).get("candle_state") == "CLOSED" else 8
        failed_direction = timeframes.get("1H", {}).get("structure", {}).get("false_breakout_direction")
        if timeframes.get("1H", {}).get("structure", {}).get("false_breakout") and failed_direction == primary_trend:
            items.append({"type": "1H_FAILED_BREAKOUT", "timeframe": "1H", "direction": failed_direction, "confirmed": timeframes.get("1H", {}).get("candle_state") == "CLOSED"})
            warning_score += 25 if timeframes.get("1H", {}).get("candle_state") == "CLOSED" else 12
        return {"score": warning_score, "items": items, "moderate": warning_score >= 15, "strong": warning_score >= 40}

    def _agreement(self, timeframes: dict[str, dict[str, Any]], primary: str) -> dict[str, Any]:
        states = {timeframe: self._trend(timeframes.get(timeframe, {}).get("structure")) for timeframe in TIMEFRAMES if timeframe in timeframes}
        available = [state for state in states.values() if state != "NEUTRAL"]
        opposites = sum(state != primary and state != "NEUTRAL" for state in available) if primary != "NEUTRAL" else 0
        if len(available) >= 4 and len(set(available)) == 1:
            status = "AGREEMENT"
        elif opposites >= 2:
            status = "HIGH_CONFLICT"
        elif opposites == 1:
            status = "CONFLICT" if any(tf in states and states[tf] != primary for tf in ("4H", "1D")) else "PARTIAL_AGREEMENT"
        else:
            status = "PARTIAL_AGREEMENT"
        return {"status": status, "states": states, "primary": primary, "contradictory_timeframes": [tf for tf, state in states.items() if primary != "NEUTRAL" and state not in {primary, "NEUTRAL"}]}

    @staticmethod
    def _holding_window(stage: str, risk: str, agreement: dict[str, Any], atr_context: dict[str, Any]) -> str:
        if stage == "REVERSAL_CONFIRMED" or risk == "VERY_HIGH":
            return "15–60M"
        if stage == "REVERSAL_WATCH" or risk == "HIGH":
            return "1–2H"
        if stage in {"EXHAUSTION_RISK", "MATURE"}:
            return "2–4H"
        if agreement["status"] == "AGREEMENT" and atr_context.get("recent_comparison") != "higher_than_recent":
            return "4–8H"
        return "2–4H"
