"""Read-only CFD trading decision layer built on the existing analysis stack.

The engines in this module interpret persisted technical evidence.  They do
not place trades, calculate probabilities, or replace the Structure and
Direction engines.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

DECISION_MODEL_VERSION = "trading_decision_v1"
PARAMETER_STATES = ("PROVISIONAL", "CALIBRATION_REQUIRED", "CALIBRATED")


@dataclass(frozen=True)
class ResearchParameter:
    value: float
    status: str = "PROVISIONAL"
    purpose: str = ""

    def __post_init__(self) -> None:
        if self.status not in PARAMETER_STATES:
            raise ValueError(f"invalid parameter status: {self.status}")


@dataclass(frozen=True)
class DecisionConfig:
    """All decision thresholds are explicit research parameters."""

    obstacle_atr_ratio: ResearchParameter = ResearchParameter(
        0.75, purpose="nearby obstacle / chase protection"
    )
    protection_atr_profit: ResearchParameter = ResearchParameter(
        0.75, purpose="meaningful open profit"
    )
    give_back_warning_ratio: ResearchParameter = ResearchParameter(
        0.35, purpose="give-back warning"
    )
    runner_room_atr: ResearchParameter = ResearchParameter(
        1.25, purpose="minimum continuation room"
    )
    runner_score: ResearchParameter = ResearchParameter(
        65.0, purpose="runner evidence qualification"
    )
    entry_score: ResearchParameter = ResearchParameter(
        65.0, purpose="entry evidence qualification"
    )
    rejection_change_ratio: ResearchParameter = ResearchParameter(
        0.20, purpose="material rebound sequence change"
    )

    def metadata(self) -> dict[str, dict[str, Any]]:
        return {name: asdict(value) for name, value in self.__dict__.items()}


class EventContextProvider(Protocol):
    def context(self, *, instrument: str, reference_time: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UnavailableEventContextProvider:
    """Safe default until a verified calendar/provider is configured."""

    def context(self, *, instrument: str, reference_time: str) -> dict[str, Any]:
        return {
            "state": "UNAVAILABLE",
            "source": None,
            "confidence_reduction_reason": "no verified event provider configured",
            "verified": False,
        }


@dataclass(frozen=True)
class StaticEventContextProvider:
    """Injection point for tests and already-verified external context."""

    state: str
    source: str | None = None
    reason: str | None = None
    verified: bool = False

    def context(self, *, instrument: str, reference_time: str) -> dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "confidence_reduction_reason": self.reason,
            "verified": self.verified,
        }


def true_vwap(
    trades: list[tuple[float, float]], *, reliable_traded_volume: bool = False
) -> dict[str, Any]:
    """Calculate VWAP only when the caller verifies actual traded volume."""
    if not reliable_traded_volume:
        return {
            "state": "UNAVAILABLE",
            "value": None,
            "reason": "reliable traded volume unavailable",
        }
    usable = [(float(price), float(volume)) for price, volume in trades if float(volume) > 0]
    total_volume = sum(volume for _, volume in usable)
    if not usable or total_volume <= 0:
        return {
            "state": "UNAVAILABLE",
            "value": None,
            "reason": "no verified positive traded volume",
        }
    return {
        "state": "AVAILABLE",
        "value": round(sum(price * volume for price, volume in usable) / total_volume, 10),
        "reason": "verified traded-volume weighted price",
    }


@dataclass
class NormalizedMarketState:
    instrument: str
    market: str | None
    epic: str | None
    source: str
    reference_time: str
    model_version: str = DECISION_MODEL_VERSION
    timeframes: dict[str, dict[str, Any]] = field(default_factory=dict)
    technical: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    patterns: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    divergence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    rejection: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    event: dict[str, Any] = field(default_factory=dict)
    position: dict[str, Any] = field(default_factory=lambda: {"state": "NONE"})
    direction: dict[str, Any] = field(default_factory=dict)
    cross_market: dict[str, Any] = field(default_factory=lambda: {"state": "UNAVAILABLE"})
    vwap: dict[str, Any] = field(
        default_factory=lambda: {
            "state": "UNAVAILABLE",
            "value": None,
            "reason": "reliable traded volume unavailable",
        }
    )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction(structure: dict[str, Any] | None) -> str:
    structure = structure or {}
    value = structure.get("current_direction") or structure.get("direction")
    if value in {"UP", "UP_STRUCTURE"}:
        return "UP"
    if value in {"DOWN", "DOWN_STRUCTURE"}:
        return "DOWN"
    labels = {
        point.get("classification")
        for point in structure.get("confirmed_highs", [])[-2:]
        + structure.get("confirmed_lows", [])[-2:]
    }
    if {"HH", "HL"} <= labels:
        return "UP"
    if {"LH", "LL"} <= labels:
        return "DOWN"
    return "NEUTRAL"


def _last_price(point_list: list[dict[str, Any]]) -> float | None:
    if not point_list:
        return None
    value = point_list[-1].get("price")
    return None if value is None else float(value)


def _side_rejection(raw: dict[str, Any], side: str) -> dict[str, Any]:
    """Return only the rejection evidence relevant to the evaluated side."""
    key = "resistance" if side.upper() == "LONG" else "support"
    return raw.get("rejection", {}).get(key, {})


class RejectionEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()

    def analyze(
        self,
        tests: list[dict[str, Any]],
        *,
        side: str,
        atr: float | None = None,
    ) -> dict[str, Any]:
        usable = [
            item
            for item in tests
            if item.get("rebound_distance") is not None
            and item.get("close_back") is not None
            and item.get("follow_through_distance") is not None
            and item.get("structure_result") is not None
        ]
        if len(usable) < 2:
            return {
                "state": "INSUFFICIENT_EVIDENCE",
                "side": side,
                "test_count": len(usable),
                "evidence": [],
            }
        rebounds = [float(item["rebound_distance"]) for item in usable]
        ratio = self.config.rejection_change_ratio.value
        increasing = all(
            right >= left * (1 + ratio)
            for left, right in zip(rebounds, rebounds[1:], strict=False)
        )
        shrinking = all(
            right <= left * (1 - ratio)
            for left, right in zip(rebounds, rebounds[1:], strict=False)
        )
        closes = sum(bool(item["close_back"]) for item in usable)
        follows = [float(item["follow_through_distance"]) for item in usable]
        if shrinking:
            state = "POSSIBLE_ABSORPTION"
        elif increasing and closes >= len(usable) - 1 and follows[-1] >= follows[0]:
            state = "STRENGTHENING_REJECTION"
        elif max(rebounds) - min(rebounds) <= max(rebounds) * ratio:
            state = "STABLE_REJECTION"
        else:
            state = "SHRINKING_REJECTION" if rebounds[-1] < rebounds[0] else "STABLE_REJECTION"
        return {
            "state": state,
            "side": side,
            "test_count": len(usable),
            "rejection_distances": rebounds,
            "follow_through_distances": follows,
            "atr_relative": None if not atr else [round(value / atr, 4) for value in rebounds],
            "evidence": ["WICK", "CLOSE", "FOLLOW_THROUGH", "STRUCTURE"],
        }


class StructureValidityEngine:
    def analyze(self, state: NormalizedMarketState | dict[str, Any], *, side: str) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        structure = raw.get("structure", {})
        primary = structure.get("1H", structure)
        side = side.upper()
        prior = primary.get("prior_direction") or _direction(primary)
        opposite_break = (
            primary.get("break_of_structure")
            and primary.get("breakout_direction") == ("UP" if side == "SHORT" else "DOWN")
        )
        explicit_break = primary.get("previous_lh_broken") if side == "SHORT" else primary.get("previous_hl_broken")
        failed_move = primary.get("failed_breakdown") if side == "SHORT" else primary.get("failed_breakout")
        failed_direction = "DOWN" if side == "SHORT" else "UP"
        failed_move = failed_move or (
            primary.get("false_breakout")
            and primary.get("false_breakout_direction") == failed_direction
        )
        # Existing false-breakout evidence already requires a close back
        # through the broken swing, so it is a verified reclaim of that level.
        reclaim = primary.get("reclaim") or primary.get("reference_reclaimed") or failed_move
        evidence = [
            name
            for name, present in (
                ("OPPOSITE_BOS", opposite_break),
                ("PREVIOUS_SWING_BROKEN", explicit_break),
                ("FAILED_CONTINUATION", failed_move),
                ("REFERENCE_RECLAIMED", reclaim),
            )
            if present
        ]
        invalid = bool(opposite_break or explicit_break or (failed_move and reclaim))
        candidate = bool(not invalid and (failed_move or reclaim))
        original_direction = "DOWN" if side == "SHORT" else "UP"
        original_valid = prior == original_direction and not invalid and not candidate
        opposite = _direction(primary)
        opposite_confirmed = opposite == ("UP" if side == "SHORT" else "DOWN") and bool(
            primary.get("opposite_trend_confirmed", False)
        )
        invalidation = (
            "BEARISH_STRUCTURE_LOST"
            if invalid and side == "SHORT"
            else "BULLISH_STRUCTURE_LOST"
            if invalid
            else "STRUCTURE_INVALIDATION_CANDIDATE"
            if candidate
            else "ORIGINAL_STRUCTURE_VALID"
            if original_valid
            else "INSUFFICIENT_EVIDENCE"
        )
        return {
            "side": side,
            "original_structure_valid": original_valid,
            "invalidation_state": invalidation,
            "invalidation_evidence": evidence,
            "recovery_conditions": [
                "re-establish structural reference",
                "confirm continuation swing sequence",
            ],
            "opposite_trend_confirmation_state": (
                "CONFIRMED" if opposite_confirmed else "NOT_CONFIRMED"
            ),
        }


class MarketRegimeEngine:
    def __init__(self, validity: StructureValidityEngine | None = None):
        self.validity = validity or StructureValidityEngine()

    def analyze(self, state: NormalizedMarketState | dict[str, Any]) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        event = raw.get("event", {})
        if event.get("verified") and event.get("state") in {"ACTIVE", "EVENT_DRIVEN"}:
            return self._result(
                "EVENT_DRIVEN",
                ["verified event context active"],
                ["ordinary technical continuation confidence reduced"],
                100.0,
                technical_confidence="REDUCED",
            )
        quality = raw.get("quality", {})
        if quality.get("candle_eligibility") == "AUDIT_ONLY" or quality.get("evidence_coverage", 0) < 0.25:
            return self._result(
                "UNCLEAR", [], ["insufficient eligible evidence"], None, technical_confidence="LOW"
            )
        structures = raw.get("structure", {})
        one_hour = structures.get("1H", {})
        down_validity = self.validity.analyze(raw, side="SHORT")
        up_validity = self.validity.analyze(raw, side="LONG")
        invalidated = {
            down_validity["invalidation_state"],
            up_validity["invalidation_state"],
        }
        if invalidated & {
            "BEARISH_STRUCTURE_LOST",
            "BULLISH_STRUCTURE_LOST",
            "STRUCTURE_INVALIDATION_CANDIDATE",
        } and not one_hour.get("opposite_trend_confirmed"):
            return self._result(
                "TRANSITION",
                down_validity["invalidation_evidence"] + up_validity["invalidation_evidence"],
                ["opposite HL/HH or LH/LL sequence not confirmed"],
                70.0,
            )
        direction = _direction(one_hour)
        if one_hour.get("breakout") and not one_hour.get("false_breakout"):
            return self._result(
                "BREAKOUT",
                [f"confirmed {one_hour['breakout']} breakout"],
                [],
                75.0,
            )
        if one_hour.get("reversal_confirmed"):
            return self._result(
                "REVERSAL", ["opposite swing sequence confirmed"], [], 80.0
            )
        if direction == "UP":
            return self._result("TREND_UP", ["1H HH/HL or UP structure"], [], 80.0)
        if direction == "DOWN":
            return self._result("TREND_DOWN", ["1H LH/LL or DOWN structure"], [], 80.0)
        if one_hour.get("direction") == "RANGE":
            return self._result("RANGE", ["1H range structure"], [], 60.0)
        return self._result("UNCLEAR", [], ["no confirmed 1H regime"], None, technical_confidence="LOW")

    @staticmethod
    def _result(
        regime: str,
        supporting: list[str],
        opposing: list[str],
        score: float | None,
        *,
        technical_confidence: str = "NORMAL",
    ) -> dict[str, Any]:
        return {
            "regime": regime,
            "regime_strength": score,
            "score_is_probability": False,
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "confirmation_conditions": ["closed-candle follow-through", "confirmed swing sequence"],
            "invalidation_conditions": ["opposite break of structure", "failed continuation"],
            "technical_confidence_state": technical_confidence,
        }


class EntryQualityEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()

    def analyze(self, state: NormalizedMarketState | dict[str, Any], *, side: str) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        side = side.upper()
        wanted = "UP" if side == "LONG" else "DOWN"
        structures = raw.get("structure", {})
        one_hour, fifteen = structures.get("1H", {}), structures.get("15M", {})
        technical = raw.get("technical", {})
        direction = raw.get("direction", {})
        quality = raw.get("quality", {})
        coverage = float(quality.get("evidence_coverage") or 0)
        if coverage < 0.25:
            return self._result(side, None, coverage, [], ["insufficient evidence"], None, "INSUFFICIENT_EVIDENCE")
        support = _last_price(one_hour.get("confirmed_lows", [])) or technical.get("support")
        resistance = _last_price(one_hour.get("confirmed_highs", [])) or technical.get("resistance")
        current = technical.get("current_price")
        obstacle = support if side == "SHORT" else resistance
        obstacle_distance = None if current is None or obstacle is None else abs(float(current) - float(obstacle))
        atr = technical.get("atr")
        supportive: list[str] = []
        opposing: list[str] = []
        score = 0.0
        for label, matched, weight in (
            ("1H structure aligned", _direction(one_hour) == wanted, 25),
            ("15M structure aligned", _direction(fifteen) == wanted, 20),
            ("direction bias aligned", direction.get("direction") == wanted, 15),
            ("breakout/retest supportive", self._trigger(one_hour, side), 15),
            ("momentum supportive", self._momentum(technical, side), 15),
            ("room before obstacle", obstacle_distance is not None and atr and obstacle_distance > atr * self.config.obstacle_atr_ratio.value, 10),
        ):
            (supportive if matched else opposing).append(label)
            if matched:
                score += weight
        nearby = bool(
            obstacle_distance is not None
            and atr
            and obstacle_distance <= atr * self.config.obstacle_atr_ratio.value
        )
        rejection = _side_rejection(raw, side).get("state")
        weakening = technical.get("momentum_state") == "weakening"
        if nearby and (weakening or rejection in {"STRENGTHENING_REJECTION", "POSSIBLE_ABSORPTION"}):
            qualifier = "DO_NOT_CHASE"
        elif nearby:
            qualifier = "POOR_LOCATION"
        elif score >= self.config.entry_score.value and self._trigger(fifteen, side):
            qualifier = "ENTRY_QUALIFIED"
        elif score >= self.config.entry_score.value:
            qualifier = "GOOD_SETUP_NOT_TRIGGERED"
        elif one_hour.get("breakout") and not one_hour.get("retest"):
            qualifier = "WAIT_FOR_RETEST"
        else:
            qualifier = "POOR_LOCATION"
        return self._result(side, score, coverage, supportive, opposing, obstacle_distance, qualifier)

    @staticmethod
    def _trigger(structure: dict[str, Any], side: str) -> bool:
        wanted = "UP" if side == "LONG" else "DOWN"
        return bool(
            structure.get("retest")
            or structure.get("reclaim")
            or (
                structure.get("breakout_direction") == wanted
                and not structure.get("false_breakout")
            )
        )

    @staticmethod
    def _momentum(technical: dict[str, Any], side: str) -> bool:
        macd = technical.get("macd", {})
        return (
            side == "LONG"
            and macd.get("polarity") == "positive"
            and macd.get("relative_to_signal") == "above"
        ) or (
            side == "SHORT"
            and macd.get("polarity") == "negative"
            and macd.get("relative_to_signal") == "below"
        )

    @staticmethod
    def _result(
        side: str,
        score: float | None,
        coverage: float,
        supporting: list[str],
        opposing: list[str],
        obstacle: float | None,
        qualifier: str,
    ) -> dict[str, Any]:
        return {
            "side_evaluated": side,
            "entry_quality_score": score,
            "score_is_probability": False,
            "evidence_coverage": coverage,
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "nearby_obstacle_distance": obstacle,
            "confirmation_trigger": "closed 15M trigger plus follow-through/retest",
            "invalidation_trigger": "opposite 15M structure break or failed trigger",
            "action_qualifier": qualifier,
        }


def position_context(
    *,
    side: str | None = None,
    entry: float | None = None,
    current_price: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    holding_duration: float | None = None,
) -> dict[str, Any]:
    if not side or entry is None or current_price is None:
        return {"state": "NONE"}
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("position side must be LONG or SHORT")
    unrealized = current_price - entry if side == "LONG" else entry - current_price
    observed_mfe = max(unrealized, 0.0) if mfe is None else mfe
    observed_mae = max(-unrealized, 0.0) if mae is None else mae
    give_back = max(observed_mfe - unrealized, 0.0)
    return {
        "state": "ACTIVE",
        "side": side,
        "entry": entry,
        "current_price": current_price,
        "unrealized_points": round(unrealized, 4),
        "mfe": observed_mfe,
        "mae": observed_mae,
        "give_back_points": round(give_back, 4),
        "give_back_ratio": None if observed_mfe <= 0 else round(give_back / observed_mfe, 4),
        "holding_duration": holding_duration,
    }


class ProfitProtectionEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()

    def analyze(
        self,
        state: NormalizedMarketState | dict[str, Any],
        validity: dict[str, Any],
    ) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        position = raw.get("position", {})
        if position.get("state") != "ACTIVE":
            return self._result("NORMAL", position, ["no active position"])
        if validity.get("invalidation_state") in {
            "BEARISH_STRUCTURE_LOST",
            "BULLISH_STRUCTURE_LOST",
        }:
            return self._result("STRUCTURE_EXIT", position, validity["invalidation_evidence"])
        profit = float(position.get("unrealized_points") or 0)
        atr = raw.get("technical", {}).get("atr")
        give_back_ratio = position.get("give_back_ratio")
        obstacle = self._obstacle_distance(raw, position.get("side"))
        weakening = raw.get("technical", {}).get("momentum_state") == "weakening"
        rejection = _side_rejection(raw, position.get("side", "")).get("state")
        meaningful = profit > 0 and (not atr or profit >= atr * self.config.protection_atr_profit.value)
        nearby = obstacle is not None and (not atr or obstacle <= atr * self.config.obstacle_atr_ratio.value)
        give_back_warning = give_back_ratio is not None and give_back_ratio >= self.config.give_back_warning_ratio.value
        if meaningful and (nearby or weakening or give_back_warning or rejection == "STRENGTHENING_REJECTION"):
            return self._result(
                "PROFIT_PROTECTION",
                position,
                [name for name, yes in (("NEARBY_OBSTACLE", nearby), ("MOMENTUM_WEAKENING", weakening), ("GIVE_BACK_WARNING", give_back_warning), ("OPPOSING_REJECTION", rejection == "STRENGTHENING_REJECTION")) if yes],
            )
        if meaningful:
            return self._result("PROFIT_DEVELOPING", position, ["meaningful unrealized profit"])
        if profit > 0:
            return self._result("PROFIT_WARNING", position, ["profit present but not developed"])
        return self._result("NORMAL", position, [])

    @staticmethod
    def _obstacle_distance(raw: dict[str, Any], side: str | None) -> float | None:
        technical = raw.get("technical", {})
        current = technical.get("current_price")
        obstacle = technical.get("resistance") if side == "LONG" else technical.get("support")
        return None if current is None or obstacle is None else abs(float(current) - float(obstacle))

    @staticmethod
    def _result(state: str, position: dict[str, Any], evidence: list[str]) -> dict[str, Any]:
        return {
            "state": state,
            "evidence": evidence,
            "unrealized_points": position.get("unrealized_points"),
            "mfe": position.get("mfe"),
            "mae": position.get("mae"),
            "give_back_points": position.get("give_back_points"),
            "give_back_ratio": position.get("give_back_ratio"),
        }


class BigWaveEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()

    def analyze(self, state: NormalizedMarketState | dict[str, Any]) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        position = raw.get("position", {})
        if position.get("state") != "ACTIVE" or float(position.get("unrealized_points") or 0) <= 0:
            return self._result("NOT_ELIGIBLE", 0, [], ["no profitable active position"])
        side = position["side"]
        wanted = "UP" if side == "LONG" else "DOWN"
        structures = raw.get("structure", {})
        technical = raw.get("technical", {})
        atr = technical.get("atr")
        current = technical.get("current_price")
        obstacle = technical.get("resistance") if side == "LONG" else technical.get("support")
        room = None if current is None or obstacle is None else abs(float(current) - float(obstacle))
        checks = (
            ("1H structure intact", _direction(structures.get("1H")) == wanted, 25),
            ("15M continuation structure", _direction(structures.get("15M")) == wanted, 20),
            ("momentum re-expanding", technical.get("momentum_state") == "strengthening", 20),
            ("volatility expanding", technical.get("volatility_state") == "expanding", 15),
            ("room before obstacle", room is not None and atr and room >= atr * self.config.runner_room_atr.value, 20),
        )
        supporting = [label for label, yes, _ in checks if yes]
        opposing = [label for label, yes, _ in checks if not yes]
        score = sum(weight for _, yes, weight in checks if yes)
        event_risk = raw.get("event", {}).get("verified") and raw.get("event", {}).get("state") in {"ACTIVE", "EVENT_DRIVEN"}
        if event_risk:
            state_name = "RUNNER_WARNING"
            opposing.append("verified event risk")
        elif score >= self.config.runner_score.value:
            state_name = "RUNNER_ELIGIBLE"
        elif score >= 40:
            state_name = "WATCH"
        else:
            state_name = "NOT_ELIGIBLE"
        return self._result(state_name, score, supporting, opposing)

    @staticmethod
    def _result(state: str, score: float, supporting: list[str], opposing: list[str]) -> dict[str, Any]:
        return {
            "big_wave_score": score,
            "score_is_probability": False,
            "qualification_state": state,
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "continuation_requirements": ["1H structure intact", "fresh 15M continuation", "room before obstacle"],
            "runner_invalidation_conditions": ["15M structure failure", "reference reclaim against position"],
        }


class TradingDecisionEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()
        self.validity = StructureValidityEngine()
        self.regime = MarketRegimeEngine(self.validity)
        self.entry = EntryQualityEngine(self.config)
        self.profit = ProfitProtectionEngine(self.config)
        self.runner = BigWaveEngine(self.config)

    def analyze(self, state: NormalizedMarketState | dict[str, Any]) -> dict[str, Any]:
        raw = state.as_dict() if isinstance(state, NormalizedMarketState) else state
        regime = self.regime.analyze(raw)
        if raw.get("position", {}).get("state") == "ACTIVE":
            raw["position"] = {
                **raw["position"],
                "nearest_support": raw.get("technical", {}).get("support"),
                "nearest_resistance": raw.get("technical", {}).get("resistance"),
                "current_regime": regime["regime"],
                "current_1H_structure": _direction(raw.get("structure", {}).get("1H")),
                "current_15M_structure": _direction(raw.get("structure", {}).get("15M")),
            }
        long_entry = self.entry.analyze(raw, side="LONG")
        short_entry = self.entry.analyze(raw, side="SHORT")
        position = raw.get("position", {"state": "NONE"})
        side = position.get("side") or ("LONG" if raw.get("direction", {}).get("direction") == "UP" else "SHORT")
        validity = self.validity.analyze(raw, side=side)
        protection = self.profit.analyze(raw, validity)
        runner = self.runner.analyze(raw)
        market_bias = self._market_bias(raw, regime)
        primary_action, machine_state = self._state_machine(
            raw, regime, long_entry, short_entry, validity, protection, runner, market_bias
        )
        result = {
            "schema_version": DECISION_MODEL_VERSION,
            "instrument": raw.get("instrument"),
            "market": raw.get("market"),
            "epic": raw.get("epic"),
            "source": raw.get("source"),
            "reference_time": raw.get("reference_time"),
            "market_bias": market_bias,
            "regime": regime,
            "structure_validity": validity,
            "entry_quality": {"LONG": long_entry, "SHORT": short_entry},
            "position": position,
            "profit_protection": protection,
            "big_wave": runner,
            "primary_action_state": primary_action,
            "decision_state": machine_state,
            "vwap": raw.get("vwap", {"state": "UNAVAILABLE", "value": None}),
            "event": raw.get("event", {}),
            "parameter_metadata": self.config.metadata(),
            "probability": None,
            "automated_execution": False,
        }
        result["telemetry"] = decision_telemetry(raw, result)
        return result

    @staticmethod
    def _market_bias(raw: dict[str, Any], regime: dict[str, Any]) -> str:
        if regime["regime"] == "TRANSITION":
            return "TRANSITION"
        direction = raw.get("direction", {}).get("direction")
        return "BULLISH" if direction == "UP" else "BEARISH" if direction == "DOWN" else "NEUTRAL"

    @staticmethod
    def _state_machine(
        raw: dict[str, Any],
        regime: dict[str, Any],
        long_entry: dict[str, Any],
        short_entry: dict[str, Any],
        validity: dict[str, Any],
        protection: dict[str, Any],
        runner: dict[str, Any],
        market_bias: str,
    ) -> tuple[str, str]:
        if regime["regime"] == "EVENT_DRIVEN":
            return "WAIT", "EVENT_RISK"
        if raw.get("position", {}).get("state") == "EXITED":
            return "EXITED", "EXITED"
        if validity["invalidation_state"] in {"BEARISH_STRUCTURE_LOST", "BULLISH_STRUCTURE_LOST"}:
            return "STRUCTURE_INVALIDATED", "STRUCTURE_INVALIDATED"
        if validity["invalidation_state"] == "STRUCTURE_INVALIDATION_CANDIDATE":
            return "WAIT", "STRUCTURE_WARNING"
        if protection["state"] == "STRUCTURE_EXIT":
            return "STRUCTURE_EXIT", "STRUCTURE_INVALIDATED"
        if protection["state"] == "PROFIT_PROTECTION":
            return "PROFIT_PROTECTION", "PROFIT_PROTECTION"
        if runner["qualification_state"] == "RUNNER_ELIGIBLE":
            return "RUNNER_ELIGIBLE", "RUNNER_ELIGIBLE"
        if raw.get("position", {}).get("state") == "ACTIVE":
            if protection["state"] == "PROFIT_DEVELOPING":
                return "HOLD_WITH_STRUCTURE", "PROFIT_DEVELOPING"
            return "MONITOR_POSITION", "POSITION_ACTIVE"
        selected = long_entry if market_bias == "BULLISH" else short_entry if market_bias == "BEARISH" else None
        if selected is None:
            return "WAIT", "SETUP_FORMING" if regime["regime"] == "TRANSITION" else "NO_TRADE"
        qualifier = selected["action_qualifier"]
        state = {
            "ENTRY_QUALIFIED": "ENTRY_QUALIFIED",
            "GOOD_SETUP_NOT_TRIGGERED": "GOOD_SETUP_NOT_TRIGGERED",
            "INSUFFICIENT_EVIDENCE": "NO_TRADE",
        }.get(qualifier, "SETUP_FORMING")
        return qualifier, state


def decision_telemetry(raw: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    technical = raw.get("technical", {})
    position = raw.get("position", {})
    structures = raw.get("structure", {})
    position_side = position.get("side")
    evaluated_side = position_side if position_side in {"LONG", "SHORT"} else {
        "BULLISH": "LONG",
        "BEARISH": "SHORT",
    }.get(decision.get("market_bias"))
    selected = decision["entry_quality"].get(evaluated_side, {}) if evaluated_side else {}
    return {
        "timestamp": raw.get("reference_time"),
        "instrument": raw.get("instrument"),
        "side": position_side,
        "evaluated_side": evaluated_side,
        "entry": position.get("entry"),
        "exit": position.get("exit"),
        "1H_regime": decision["regime"]["regime"],
        "regime_strength": decision["regime"].get("regime_strength"),
        "market_bias": decision.get("market_bias"),
        "15M_regime": _direction(structures.get("15M")),
        "1H_structure": _direction(structures.get("1H")),
        "15M_structure": _direction(structures.get("15M")),
        "structure_invalidation_state": decision["structure_validity"].get("invalidation_state"),
        "structure_original_valid": decision["structure_validity"].get("original_structure_valid"),
        "atr": technical.get("atr"),
        "bollinger_state": technical.get("bollinger", {}).get("zone"),
        "vwap_state": raw.get("vwap", {}).get("state", "UNAVAILABLE"),
        "ema_state": technical.get("ema_state"),
        "macd_state": technical.get("macd"),
        "rsi": technical.get("rsi"),
        "nearest_support_distance": technical.get("support_distance"),
        "nearest_resistance_distance": technical.get("resistance_distance"),
        "rejection_state": raw.get("rejection", {}).get("state"),
        "support_rejection_state": raw.get("rejection", {}).get("support", {}).get("state"),
        "resistance_rejection_state": raw.get("rejection", {}).get("resistance", {}).get("state"),
        "gap_state": technical.get("gap_state"),
        "event_state": raw.get("event", {}).get("state"),
        "direction_score": raw.get("direction", {}).get("up_score") if evaluated_side == "LONG" else raw.get("direction", {}).get("down_score") if evaluated_side == "SHORT" else None,
        "entry_quality_score": selected.get("entry_quality_score"),
        "entry_quality_qualifier": selected.get("action_qualifier"),
        "long_entry_quality_score": decision["entry_quality"].get("LONG", {}).get("entry_quality_score"),
        "short_entry_quality_score": decision["entry_quality"].get("SHORT", {}).get("entry_quality_score"),
        "big_wave_score": decision["big_wave"].get("big_wave_score"),
        "big_wave_qualification_state": decision["big_wave"].get("qualification_state"),
        "profit_protection_state": decision["profit_protection"].get("state"),
        "primary_action_state": decision["primary_action_state"],
        "decision_state": decision.get("decision_state"),
        "position_state": position.get("state"),
        "current_price": position.get("current_price"),
        "unrealized_points": position.get("unrealized_points"),
        "mfe": position.get("mfe"),
        "mae": position.get("mae"),
        "realized_points": position.get("realized_points"),
        "maximum_unrealized_profit": position.get("mfe"),
        "give_back_points": position.get("give_back_points"),
        "give_back_ratio": position.get("give_back_ratio"),
        "holding_duration": position.get("holding_duration"),
        "reason_for_exit": position.get("reason_for_exit"),
        "model_version": raw.get("model_version", DECISION_MODEL_VERSION),
        "source": raw.get("source"),
        "provenance": raw.get("quality", {}).get("provenance"),
        "risk_reward": position.get("risk_reward"),
        "drawdown": position.get("drawdown"),
        "consecutive_losses": position.get("consecutive_losses"),
        "time_of_day": raw.get("reference_time", "")[11:19] or None,
        "event_day": raw.get("event", {}).get("verified", False),
    }


def normalized_state_from_database(
    database: Any,
    instrument_id: str,
    *,
    event_provider: EventContextProvider | None = None,
    position: dict[str, Any] | None = None,
) -> NormalizedMarketState:
    """Build one shared point-in-time state from existing persisted engines."""
    instruments = {row[0]: row for row in database.list_instruments()}
    identity = instruments.get(instrument_id)
    if identity is None:
        raise ValueError(f"unknown instrument: {instrument_id}")
    analyses: dict[str, dict[str, Any]] = {}
    patterns: dict[str, list[dict[str, Any]]] = {}
    divergences: dict[str, list[dict[str, Any]]] = {}
    technical_features: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for timeframe in ("15M", "1H", "4H", "1D"):
        status = database.get_phase2b_status(instrument_id, timeframe)
        features = database.get_technical_features(instrument_id, timeframe)
        technical_features[timeframe] = features[0] if features else {}
        analyses[timeframe] = status.get("structure") or {}
        observations = status.get("patterns") or []
        divergences[timeframe] = [
            item for item in observations if "Divergence" in str(item.get("pattern", ""))
        ]
        patterns[timeframe] = [
            item for item in observations if item not in divergences[timeframe]
        ]
        if not analyses[timeframe] or not features:
            missing.append(timeframe)
    one = technical_features["1H"]
    candle = one.get("candle", {})
    sr = one.get("support_resistance", {})
    reference_time = one.get("candle_timestamp")
    if reference_time is None:
        direction = database.get_direction_status(instrument_id) or {}
        reference_time = direction.get("candle_timestamp") or datetime.now(UTC).isoformat()
    direction = database.get_direction_status(instrument_id) or {}
    latest_observation = database.connection.execute(
        "SELECT observed_at, source FROM observations WHERE instrument_id=? ORDER BY observed_at DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    quality_row = database.connection.execute(
        "SELECT eligibility FROM candle_quality WHERE instrument_id=? AND timeframe='1H' ORDER BY start_at DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    current = candle.get("close")
    support = sr.get("rolling_low")
    resistance = sr.get("rolling_high")
    event = (event_provider or UnavailableEventContextProvider()).context(
        instrument=instrument_id, reference_time=reference_time
    )
    support_tests = _level_tests(
        database.list_candles(instrument_id, "15M", limit=40),
        level=support,
        side="SUPPORT",
        atr=one.get("atr14", {}).get("value"),
    )
    resistance_tests = _level_tests(
        database.list_candles(instrument_id, "15M", limit=40),
        level=resistance,
        side="RESISTANCE",
        atr=one.get("atr14", {}).get("value"),
    )
    rejection_engine = RejectionEngine()
    support_rejection = rejection_engine.analyze(
        support_tests, side="SUPPORT", atr=one.get("atr14", {}).get("value")
    )
    resistance_rejection = rejection_engine.analyze(
        resistance_tests, side="RESISTANCE", atr=one.get("atr14", {}).get("value")
    )
    directional_rejection = (
        support_rejection if direction.get("direction") == "DOWN" else resistance_rejection
    )
    state = NormalizedMarketState(
        instrument=instrument_id,
        market=identity[2],
        epic=identity[1],
        source=(latest_observation[1] if latest_observation else "PERSISTED_ANALYSIS"),
        reference_time=reference_time,
        timeframes={
            tf: {
                "direction": _direction(analyses[tf]),
                "structure": analyses[tf],
                "candle_state": technical_features[tf].get("candle_state"),
            }
            for tf in analyses
        },
        structure=analyses,
        patterns=patterns,
        divergence=divergences,
        rejection={
            **directional_rejection,
            "support": support_rejection,
            "resistance": resistance_rejection,
        },
        technical={
            "current_price": current,
            "atr": one.get("atr14", {}).get("value"),
            "bollinger": one.get("bollinger", {}),
            "ema_state": one.get("ema_ordering", {}),
            "macd": one.get("macd", {}),
            "momentum_state": one.get("macd", {}).get("histogram_state"),
            "volatility_state": "expanding" if one.get("atr14", {}).get("recent_comparison") == "higher_than_recent" else "contracting",
            "rsi": one.get("rsi14", {}).get("value"),
            "support": support,
            "resistance": resistance,
            "support_distance": None if current is None or support is None else current - support,
            "resistance_distance": None if current is None or resistance is None else resistance - current,
            "gap_state": one.get("gaps", [])[-1] if one.get("gaps") else None,
        },
        quality={
            "candle_eligibility": quality_row[0] if quality_row else "UNKNOWN",
            "data_freshness": latest_observation[0] if latest_observation else None,
            "evidence_coverage": round((4 - len(missing)) / 4, 2),
            "missing_data_flags": missing,
            "provenance": database.research_source_identity(instrument_id),
        },
        event=event,
        position=position or {"state": "NONE"},
        direction=direction,
    )
    return state


def _level_tests(
    candles: list[Any],
    *,
    level: float | None,
    side: str,
    atr: float | None,
) -> list[dict[str, Any]]:
    """Extract wick+close+follow-through+structure evidence around a level."""
    if level is None or len(candles) < 2:
        return []
    threshold = max((float(atr) * 0.25) if atr else 0.0, abs(float(level)) * 0.0005)
    output: list[dict[str, Any]] = []
    for index, candle in enumerate(candles[:-1]):
        low, high, close = float(candle.low), float(candle.high), float(candle.close)
        if side == "SUPPORT":
            tested = low <= float(level) + threshold
            close_back = close >= float(level)
            rejection_distance = max(close - min(low, float(level)), 0.0)
            future = candles[index + 1 : index + 4]
            follow = max((float(item.high) for item in future), default=close) - close
            held = all(float(item.close) >= float(level) for item in future)
        else:
            tested = high >= float(level) - threshold
            close_back = close <= float(level)
            rejection_distance = max(max(high, float(level)) - close, 0.0)
            future = candles[index + 1 : index + 4]
            follow = close - min((float(item.low) for item in future), default=close)
            held = all(float(item.close) <= float(level) for item in future)
        if tested:
            output.append(
                {
                    "rebound_distance": rejection_distance,
                    "close_back": close_back,
                    "follow_through_distance": max(follow, 0.0),
                    "structure_result": "HELD" if held else "BROKE",
                    "timestamp": candle.start.isoformat(),
                }
            )
    return output[-6:]
