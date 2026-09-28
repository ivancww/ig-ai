"""Deterministic Phase 4A monitoring state and alert engine."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ALERT_ENGINE_VERSION = "alert_engine_v1"
PRIORITIES = ("INFO", "WATCH", "WARNING", "CRITICAL")
RISK_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "VERY_HIGH": 3}
STAGE_RANK = {"EARLY": 0, "CONFIRMED": 1, "MATURE": 2, "EXHAUSTION_RISK": 3, "REVERSAL_WATCH": 4, "REVERSAL_CONFIRMED": 5}


@dataclass(frozen=True)
class AlertConfig:
    version: str = ALERT_ENGINE_VERSION
    score_change_threshold: float = 5.0
    cooldown_seconds: int = 300


def monitor_state(direction: dict[str, Any], coordinated: dict[str, Any]) -> dict[str, Any]:
    """Build the accepted comparison state without copying unbounded history."""
    analyses = coordinated.get("timeframes", {})
    patterns = {}
    divergences = {}
    structures = {}
    volatility = {}
    early_warning = []
    for timeframe, analysis in analyses.items():
        structures[timeframe] = {
            "direction": analysis.get("structure", {}).get("direction"),
            "current_direction": analysis.get("structure", {}).get("current_direction"),
            "prior_direction": analysis.get("structure", {}).get("prior_direction"),
            "breakout": analysis.get("structure", {}).get("breakout"),
            "breakout_direction": analysis.get("structure", {}).get("breakout_direction"),
            "break_of_structure": analysis.get("structure", {}).get("break_of_structure", False),
            "false_breakout": analysis.get("structure", {}).get("false_breakout", False),
            "false_breakout_direction": analysis.get("structure", {}).get("false_breakout_direction"),
            "candle_state": analysis.get("candle_state"),
        }
        volatility[timeframe] = analysis.get("context", {}).get("atr", {}).get("recent_comparison")
        for pattern in analysis.get("candlestick_patterns", []) + analysis.get("chart_patterns", []):
            identity = pattern.get("instance_id") or f"{pattern.get('pattern')}:{pattern.get('start')}"
            patterns[f"{timeframe}:{identity}"] = {"pattern": pattern.get("pattern"), "lifecycle": pattern.get("lifecycle"), "confirmed": pattern.get("candle_state") == "CLOSED"}
        divergences[timeframe] = [{"pattern": item.get("pattern"), "start": item.get("start"), "end": item.get("end"), "lifecycle": item.get("lifecycle")} for item in analysis.get("divergences", [])]
        if timeframe == "15M":
            early_warning.extend(item.get("pattern") for item in analysis.get("divergences", []) if item.get("lifecycle") in {"FORMING", "CONFIRMED"})
    return {
        "schema_version": ALERT_ENGINE_VERSION,
        "instrument": direction.get("instrument"),
        "model_reference": direction.get("model_reference", {"timeframe": "1H", "candle_timestamp": direction.get("candle_timestamp"), "candle_state": direction.get("candle_state")} ),
        "trigger": direction.get("trigger"),
        "direction": direction.get("direction"),
        "up_score": direction.get("up_score"),
        "down_score": direction.get("down_score"),
        "coverage": direction.get("coverage"),
        "trend_stage": direction.get("trend_stage"),
        "reversal_risk": direction.get("reversal_risk"),
        "holding_window": direction.get("holding_window"),
        "timeframe_agreement": direction.get("timeframe_agreement"),
        "evidence_ledger": direction.get("evidence_ledger", []),
        "structures": structures,
        "patterns": patterns,
        "divergences": divergences,
        "volatility": volatility,
        "early_warning": sorted(set(early_warning)),
        "score_version": direction.get("score_version"),
    }


class AlertEngine:
    def __init__(self, config: AlertConfig | None = None):
        self.config = config or AlertConfig()
        self._last_emitted: dict[str, tuple[datetime, int]] = {}

    @staticmethod
    def _priority_rank(priority: str) -> int:
        return PRIORITIES.index(priority)

    def _event(self, instrument: str, alert_type: str, priority: str, previous: dict[str, Any], current: dict[str, Any], identity: Any, evidence: list[str], confirmed: bool, now: datetime) -> dict[str, Any]:
        event_identity = f"{instrument}:{alert_type}:{json.dumps(identity, sort_keys=True, separators=(',', ':'))}"
        alert_id = hashlib.sha256(event_identity.encode()).hexdigest()
        previous_risk = (previous.get("reversal_risk") or {}).get("category")
        current_risk = (current.get("reversal_risk") or {}).get("category")
        message = self._message(alert_type, priority, previous, current, evidence, confirmed)
        return {"alert_id": alert_id, "instrument": instrument, "alert_type": alert_type, "priority": priority, "event_identity": event_identity, "created_at": now.isoformat(), "model_reference_time": (current.get("model_reference") or {}).get("candle_timestamp"), "trigger_time": (current.get("trigger") or {}).get("candle_timestamp"), "previous_direction": previous.get("direction"), "current_direction": current.get("direction"), "previous_score": previous.get("up_score") if previous.get("direction") == "UP" else previous.get("down_score"), "current_score": current.get("up_score") if current.get("direction") == "UP" else current.get("down_score"), "previous_trend_stage": previous.get("trend_stage"), "current_trend_stage": current.get("trend_stage"), "previous_reversal_risk": previous_risk, "current_reversal_risk": current_risk, "previous_holding_window": previous.get("holding_window"), "current_holding_window": current.get("holding_window"), "timeframe_agreement": current.get("timeframe_agreement"), "trigger_evidence": evidence, "confirmed": confirmed, "state": "CLOSED/CONFIRMED" if confirmed else "FORMING/PROVISIONAL", "score_version": current.get("score_version"), "alert_engine_version": self.config.version, "message": message}

    @staticmethod
    def _message(alert_type: str, priority: str, previous: dict[str, Any], current: dict[str, Any], evidence: list[str], confirmed: bool) -> str:
        lines = [f"{current.get('instrument')} — {alert_type.replace('_', ' ').title()}", f"Priority: {priority}", f"1H Direction: {current.get('direction')}", f"Trend Stage: {current.get('trend_stage')}", f"Reversal Risk: {(current.get('reversal_risk') or {}).get('category')}"]
        if previous.get("direction") != current.get("direction"):
            lines.append(f"Direction: {previous.get('direction')} -> {current.get('direction')}")
        if evidence:
            lines.append("Evidence: " + "; ".join(evidence))
        lines.append("Status: CLOSED/CONFIRMED" if confirmed else "Status: FORMING/PROVISIONAL")
        lines.append("Technical model evidence only — no trading recommendation.")
        return "\n".join(lines)

    def _emit(self, output: list[dict[str, Any]], *, instrument: str, alert_type: str, priority: str, previous: dict[str, Any], current: dict[str, Any], identity: Any, evidence: list[str], confirmed: bool, now: datetime) -> None:
        alert = self._event(instrument, alert_type, priority, previous, current, identity, evidence, confirmed, now)
        previous_sent = self._last_emitted.get(alert["event_identity"])
        if previous_sent and (now - previous_sent[0]).total_seconds() < self.config.cooldown_seconds and self._priority_rank(priority) <= previous_sent[1]:
            return
        self._last_emitted[alert["event_identity"]] = (now, self._priority_rank(priority))
        output.append(alert)

    def evaluate(self, previous: dict[str, Any] | None, current: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
        if not current.get("instrument"):
            return []
        now = now or datetime.now(UTC)
        previous = previous or {}
        output: list[dict[str, Any]] = []
        instrument = current["instrument"]
        confirmed = (current.get("model_reference") or {}).get("candle_state") == "CLOSED"
        if previous and previous.get("direction") != current.get("direction"):
            priority = "CRITICAL" if current.get("trend_stage") == "REVERSAL_CONFIRMED" and confirmed else "WARNING"
            self._emit(output, instrument=instrument, alert_type="DIRECTION_SHIFT", priority=priority, previous=previous, current=current, identity=[previous.get("direction"), current.get("direction")], evidence=["1H direction changed"], confirmed=confirmed, now=now)
        if previous and previous.get("trend_stage") != current.get("trend_stage"):
            stage = current.get("trend_stage")
            priority = "CRITICAL" if stage == "REVERSAL_CONFIRMED" and confirmed else "WARNING" if stage in {"EXHAUSTION_RISK", "REVERSAL_WATCH"} else "WATCH" if stage == "MATURE" else "INFO"
            alert_type = "REVERSAL_CONFIRMED" if stage == "REVERSAL_CONFIRMED" and confirmed else "REVERSAL_WATCH" if stage == "REVERSAL_WATCH" else "TREND_STAGE_CHANGE"
            self._emit(output, instrument=instrument, alert_type=alert_type, priority=priority, previous=previous, current=current, identity=[previous.get("trend_stage"), stage], evidence=[f"Trend stage {previous.get('trend_stage')} -> {stage}"], confirmed=confirmed, now=now)
        old_risk = RISK_RANK.get((previous.get("reversal_risk") or {}).get("category"), -1)
        new_risk = RISK_RANK.get((current.get("reversal_risk") or {}).get("category"), -1)
        if previous and new_risk != old_risk:
            alert_type = "REVERSAL_RISK_INCREASE" if new_risk > old_risk else "REVERSAL_RISK_DECREASE"
            priority = "WARNING" if new_risk >= 2 else "WATCH" if new_risk > old_risk else "INFO"
            self._emit(output, instrument=instrument, alert_type=alert_type, priority=priority, previous=previous, current=current, identity=[old_risk, new_risk], evidence=[f"Reversal risk {(previous.get('reversal_risk') or {}).get('category')} -> {(current.get('reversal_risk') or {}).get('category')}"], confirmed=confirmed, now=now)
        previous_warning = set(previous.get("early_warning", []))
        current_warning = set(current.get("early_warning", [])) - previous_warning
        if current_warning and current.get("direction") in {"UP", "DOWN"} and current.get("trend_stage") not in {"REVERSAL_CONFIRMED"}:
            self._emit(output, instrument=instrument, alert_type="EARLY_REVERSAL_WARNING", priority="WATCH", previous=previous, current=current, identity=sorted(current_warning), evidence=sorted(current_warning) + ["1H structure remains primary"], confirmed=confirmed, now=now)
        previous_agreement = (previous.get("timeframe_agreement") or {}).get("status")
        current_agreement = (current.get("timeframe_agreement") or {}).get("status")
        if previous and previous_agreement != current_agreement:
            alert_type = "TIMEFRAME_REALIGNMENT" if current_agreement == "AGREEMENT" else "TIMEFRAME_CONFLICT"
            priority = "INFO" if alert_type == "TIMEFRAME_REALIGNMENT" else "WATCH"
            self._emit(output, instrument=instrument, alert_type=alert_type, priority=priority, previous=previous, current=current, identity=[previous_agreement, current_agreement], evidence=[f"Timeframe agreement {previous_agreement} -> {current_agreement}"], confirmed=confirmed, now=now)
        if previous:
            score_delta = abs(float(current.get("up_score") or 0) - float(previous.get("up_score") or 0)) if current.get("direction") == "UP" else abs(float(current.get("down_score") or 0) - float(previous.get("down_score") or 0))
            if score_delta >= self.config.score_change_threshold and current.get("direction") == previous.get("direction") and current.get("trend_stage") != previous.get("trend_stage"):
                self._emit(output, instrument=instrument, alert_type="DIRECTION_SHIFT", priority="WARNING", previous=previous, current=current, identity=["material", round(score_delta, 2)], evidence=[f"Score delta {round(score_delta, 2)} with structural state change"], confirmed=confirmed, now=now)
        old_structures, new_structures = previous.get("structures", {}), current.get("structures", {})
        primary = new_structures.get("1H", {})
        if primary.get("false_breakout") and not old_structures.get("1H", {}).get("false_breakout"):
            self._emit(output, instrument=instrument, alert_type="FALSE_BREAKOUT", priority="WARNING", previous=previous, current=current, identity=["1H", primary.get("false_breakout_direction")], evidence=["1H false breakout"], confirmed=confirmed, now=now)
        if primary.get("breakout") and not primary.get("false_breakout") and primary.get("breakout") != old_structures.get("1H", {}).get("breakout"):
            self._emit(output, instrument=instrument, alert_type="BREAKOUT_CONFIRMED", priority="WATCH", previous=previous, current=current, identity=["1H", primary.get("breakout_direction")], evidence=["1H breakout confirmed"], confirmed=confirmed, now=now)
            boundary_type = "RESISTANCE_BREAK" if primary.get("breakout_direction") == "UP" else "SUPPORT_BREAK" if primary.get("breakout_direction") == "DOWN" else None
            if boundary_type:
                self._emit(output, instrument=instrument, alert_type=boundary_type, priority="WATCH", previous=previous, current=current, identity=["1H", primary.get("breakout_direction")], evidence=[f"1H {boundary_type.replace('_', ' ').lower()}"], confirmed=confirmed, now=now)
        old_patterns, new_patterns = previous.get("patterns", {}), current.get("patterns", {})
        for identity, pattern in new_patterns.items():
            old_lifecycle = old_patterns.get(identity, {}).get("lifecycle")
            lifecycle = pattern.get("lifecycle")
            if lifecycle != old_lifecycle and lifecycle in {"NEAR_CONFIRMATION", "CONFIRMED", "FAILED"}:
                alert_type = "PATTERN_NEAR_CONFIRMATION" if lifecycle == "NEAR_CONFIRMATION" else "PATTERN_CONFIRMED" if lifecycle == "CONFIRMED" else "PATTERN_FAILED"
                priority = "WATCH" if lifecycle == "NEAR_CONFIRMATION" else "WARNING" if lifecycle == "FAILED" else "INFO"
                self._emit(output, instrument=instrument, alert_type=alert_type, priority=priority, previous=previous, current=current, identity=[identity, lifecycle], evidence=[f"{pattern.get('pattern')} {old_lifecycle} -> {lifecycle}"], confirmed=bool(pattern.get("confirmed")), now=now)
        for timeframe, value in current.get("volatility", {}).items():
            if value == "higher_than_recent" and previous.get("volatility", {}).get(timeframe) != value:
                self._emit(output, instrument=instrument, alert_type="VOLATILITY_EXPANSION", priority="WATCH", previous=previous, current=current, identity=[timeframe, value], evidence=[f"{timeframe} ATR higher than recent history"], confirmed=timeframe == "1H" and confirmed, now=now)
        if previous and previous.get("holding_window") != current.get("holding_window"):
            window_rank = {"15–60M": 0, "1–2H": 1, "2–4H": 2, "4–8H": 3, "8H+": 4}
            shorter = window_rank.get(current.get("holding_window"), 2) < window_rank.get(previous.get("holding_window"), 2)
            alert_type = "HOLDING_WINDOW_SHORTENED" if shorter else "HOLDING_WINDOW_EXTENDED"
            self._emit(output, instrument=instrument, alert_type=alert_type, priority="WARNING" if shorter else "INFO", previous=previous, current=current, identity=[previous.get("holding_window"), current.get("holding_window")], evidence=[f"Holding window {previous.get('holding_window')} -> {current.get('holding_window')}"], confirmed=confirmed, now=now)
        return output
