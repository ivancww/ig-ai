import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from ig_ai.alerts import ALERT_ENGINE_VERSION, AlertConfig, AlertEngine, monitor_state
from ig_ai.database import Database

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def state(*, direction="UP", score=70, stage="CONFIRMED", risk="MODERATE", holding="4–8H", agreement="AGREEMENT", closed=True, warnings=None, structures=None, patterns=None, volatility=None):
    return {
        "instrument": "US Tech 100", "schema_version": ALERT_ENGINE_VERSION,
        "model_reference": {"timeframe": "1H", "candle_timestamp": "2026-01-01T00:00:00+00:00", "candle_state": "CLOSED" if closed else "FORMING"},
        "trigger": {"timeframe": "15M", "candle_timestamp": "2026-01-01T00:15:00+00:00", "candle_state": "CLOSED" if closed else "FORMING"},
        "direction": direction, "up_score": score if direction == "UP" else 30, "down_score": score if direction == "DOWN" else 30,
        "coverage": {"ratio": 0.8}, "trend_stage": stage, "reversal_risk": {"category": risk, "score": 50 if risk == "HIGH" else 20, "evidence": []}, "holding_window": holding,
        "timeframe_agreement": {"status": agreement, "states": {}}, "evidence_ledger": [], "structures": structures or {"1H": {"breakout": None, "false_breakout": False}}, "patterns": patterns or {}, "divergences": {}, "volatility": volatility or {"1H": "lower_than_recent"}, "early_warning": warnings or [], "score_version": "direction_score_v1",
    }


def test_tiny_score_change_does_not_alert():
    engine = AlertEngine()
    previous = state(score=70)
    current = state(score=72)
    assert engine.evaluate(previous, current, now=NOW) == []


def test_direction_shift_and_confirmed_reversal_are_critical():
    engine = AlertEngine()
    previous = state(direction="UP", score=70, stage="REVERSAL_WATCH", risk="HIGH", holding="4–8H")
    current = state(direction="DOWN", score=72, stage="REVERSAL_CONFIRMED", risk="VERY_HIGH", holding="15–60M")
    alerts = engine.evaluate(previous, current, now=NOW)
    types = {item["alert_type"] for item in alerts}
    assert {"DIRECTION_SHIFT", "REVERSAL_CONFIRMED", "REVERSAL_RISK_INCREASE", "HOLDING_WINDOW_SHORTENED"} <= types
    assert all(item["priority"] == "CRITICAL" for item in alerts if item["alert_type"] in {"DIRECTION_SHIFT", "REVERSAL_CONFIRMED"})


def test_stage_escalation_and_risk_decrease_are_deterministic():
    engine = AlertEngine()
    assert any(item["alert_type"] == "TREND_STAGE_CHANGE" for item in engine.evaluate(state(stage="CONFIRMED"), state(stage="MATURE"), now=NOW))
    assert any(item["alert_type"] == "TREND_STAGE_CHANGE" for item in engine.evaluate(state(stage="MATURE"), state(stage="EXHAUSTION_RISK", risk="HIGH"), now=NOW + timedelta(minutes=1)))
    assert any(item["alert_type"] == "REVERSAL_WATCH" for item in engine.evaluate(state(stage="EXHAUSTION_RISK", risk="HIGH"), state(stage="REVERSAL_WATCH", risk="HIGH"), now=NOW + timedelta(minutes=2)))
    assert any(item["alert_type"] == "REVERSAL_RISK_DECREASE" for item in engine.evaluate(state(risk="HIGH"), state(risk="MODERATE"), now=NOW + timedelta(minutes=3)))


def test_bullish_and_bearish_early_warnings_are_provisional_when_forming():
    engine = AlertEngine()
    bullish = engine.evaluate(state(direction="UP"), state(direction="UP", stage="MATURE", warnings=["Bearish RSI Divergence"], closed=False), now=NOW)
    bearish = engine.evaluate(state(direction="DOWN"), state(direction="DOWN", stage="MATURE", warnings=["Bullish MACD Divergence"], closed=False), now=NOW)
    bullish_warning = next(item for item in bullish if item["alert_type"] == "EARLY_REVERSAL_WARNING")
    bearish_warning = next(item for item in bearish if item["alert_type"] == "EARLY_REVERSAL_WARNING")
    assert bullish_warning["state"] == "FORMING/PROVISIONAL"
    assert bearish_warning["state"] == "FORMING/PROVISIONAL"


def test_forming_1h_cannot_emit_confirmed_reversal_alert():
    engine = AlertEngine()
    alerts = engine.evaluate(state(stage="REVERSAL_WATCH", risk="HIGH"), state(direction="DOWN", stage="REVERSAL_CONFIRMED", risk="VERY_HIGH", holding="15–60M", closed=False), now=NOW)
    assert not any(item["alert_type"] == "REVERSAL_CONFIRMED" and item["priority"] == "CRITICAL" for item in alerts)


def test_conflict_realign_breakout_false_breakout_and_volatility():
    engine = AlertEngine()
    previous = state(agreement="AGREEMENT", structures={"1H": {"breakout": None, "false_breakout": False}}, volatility={"1H": "lower_than_recent"})
    current = state(agreement="CONFLICT", structures={"1H": {"breakout": "up", "breakout_direction": "UP", "false_breakout": False}}, volatility={"1H": "higher_than_recent"})
    alerts = engine.evaluate(previous, current, now=NOW)
    assert {item["alert_type"] for item in alerts} >= {"TIMEFRAME_CONFLICT", "BREAKOUT_CONFIRMED", "VOLATILITY_EXPANSION"}
    failed = state(structures={"1H": {"breakout": "up", "breakout_direction": "UP", "false_breakout": True, "false_breakout_direction": "UP"}})
    assert any(item["alert_type"] == "FALSE_BREAKOUT" for item in engine.evaluate(current, failed, now=NOW + timedelta(minutes=1)))


def test_support_and_resistance_break_alerts_have_deterministic_positive_paths():
    engine = AlertEngine()
    old = state(structures={"1H": {"breakout": None, "false_breakout": False}})
    resistance = state(structures={"1H": {"breakout": "up", "breakout_direction": "UP", "false_breakout": False, "candle_state": "CLOSED"}})
    support = state(direction="DOWN", structures={"1H": {"breakout": "down", "breakout_direction": "DOWN", "false_breakout": False, "candle_state": "CLOSED"}})
    assert any(item["alert_type"] == "RESISTANCE_BREAK" for item in engine.evaluate(old, resistance, now=NOW))
    assert any(item["alert_type"] == "SUPPORT_BREAK" for item in engine.evaluate(old, support, now=NOW + timedelta(minutes=1)))


def test_pattern_lifecycle_and_holding_window_alerts():
    engine = AlertEngine()
    previous = state(patterns={"1H:Double Top:x": {"pattern": "Double Top", "lifecycle": "POTENTIAL", "confirmed": False}}, holding="4–8H")
    current = state(stage="MATURE", patterns={"1H:Double Top:x": {"pattern": "Double Top", "lifecycle": "CONFIRMED", "confirmed": True}}, holding="2–4H")
    alerts = engine.evaluate(previous, current, now=NOW)
    assert any(item["alert_type"] == "PATTERN_CONFIRMED" for item in alerts)
    assert any(item["alert_type"] == "HOLDING_WINDOW_SHORTENED" for item in alerts)
    extended = engine.evaluate(state(holding="2–4H"), state(holding="4–8H"), now=NOW + timedelta(minutes=1))
    assert any(item["alert_type"] == "HOLDING_WINDOW_EXTENDED" for item in extended)


def test_pattern_near_confirmation_and_failure_alerts():
    engine = AlertEngine()
    potential = state(patterns={"1H:Triangle:one": {"pattern": "Triangle", "lifecycle": "POTENTIAL", "confirmed": False}})
    near = state(patterns={"1H:Triangle:one": {"pattern": "Triangle", "lifecycle": "NEAR_CONFIRMATION", "confirmed": True, "candle_state": "CLOSED"}})
    failed = state(patterns={"1H:Triangle:one": {"pattern": "Triangle", "lifecycle": "FAILED", "confirmed": True, "candle_state": "CLOSED"}})
    assert any(item["alert_type"] == "PATTERN_NEAR_CONFIRMATION" for item in engine.evaluate(potential, near, now=NOW))
    assert any(item["alert_type"] == "PATTERN_FAILED" for item in engine.evaluate(near, failed, now=NOW + timedelta(minutes=1)))


def test_dedup_cooldown_and_escalation_override():
    engine = AlertEngine(AlertConfig(cooldown_seconds=300))
    previous, current = state(stage="CONFIRMED"), state(stage="MATURE")
    first = engine.evaluate(previous, current, now=NOW)
    repeated = engine.evaluate(previous, current, now=NOW + timedelta(minutes=1))
    escalated = engine.evaluate(previous, state(stage="REVERSAL_WATCH", risk="HIGH"), now=NOW + timedelta(minutes=1))
    assert len(first) == 1
    assert repeated == []
    assert escalated
    assert first[0]["alert_id"] != escalated[0]["alert_id"]


def test_monitor_state_normalization_and_restart_persistence(tmp_path):
    direction = {"instrument": "US Tech 100", "model_reference": {"timeframe": "1H", "candle_timestamp": "2026-01-01T00:00:00+00:00", "candle_state": "CLOSED"}, "direction": "UP", "up_score": 70, "down_score": 30, "trend_stage": "CONFIRMED", "reversal_risk": {"category": "MODERATE"}, "holding_window": "4–8H", "timeframe_agreement": {"status": "AGREEMENT"}, "score_version": "direction_score_v1"}
    normalized = monitor_state(direction, {"timeframes": {}})
    database = Database(tmp_path / "alerts.sqlite")
    database.save_monitor_state(normalized)
    alert = AlertEngine().evaluate(None, normalized, now=NOW)
    assert alert == []
    database.close()
    reopened = Database(tmp_path / "alerts.sqlite")
    assert reopened.get_monitor_state("US Tech 100")["direction"] == "UP"
    assert reopened.get_monitor_status("US Tech 100")["alert_count"] == 0
    reopened.close()


def test_alert_payload_persistence_is_idempotent_and_three_market_isolated(tmp_path):
    database = Database(tmp_path / "alerts.sqlite")
    engine = AlertEngine()
    for instrument in ("US Tech 100", "Japan 225", "Hong Kong HS50"):
        previous = state()
        previous["instrument"] = instrument
        current = state(stage="MATURE", holding="2–4H")
        current["instrument"] = instrument
        alert = engine.evaluate(previous, current, now=NOW)[0]
        assert database.save_alert(alert) is True
        assert database.save_alert(alert) is False
    assert len(database.list_alerts(limit=10)) == 3
    assert len(database.list_alerts("Japan 225")) == 1
    assert database.connection.execute("SELECT COUNT(*) FROM alert_delivery_state").fetchone()[0] == 3
    database.close()


def test_restart_restores_cooldown_and_allows_escalation(tmp_path):
    database = Database(tmp_path / "restart-alerts.sqlite")
    first_engine = AlertEngine(AlertConfig(cooldown_seconds=300))
    previous = state(stage="CONFIRMED")
    current = state(stage="MATURE")
    first = first_engine.evaluate(previous, current, now=NOW)
    database.save_monitor_evaluation(current, first)

    restarted = AlertEngine(AlertConfig(cooldown_seconds=300))
    restarted.restore(database.list_alerts(limit=100))
    assert restarted.evaluate(previous, current, now=NOW + timedelta(minutes=1)) == []
    escalation = restarted.evaluate(
        state(stage="MATURE"),
        state(stage="REVERSAL_CONFIRMED", direction="DOWN", risk="VERY_HIGH", holding="15–60M"),
        now=NOW + timedelta(minutes=1),
    )
    assert any(item["alert_type"] == "REVERSAL_CONFIRMED" for item in escalation)
    database.close()


def test_transition_identity_includes_reference_and_preserves_later_event(tmp_path):
    database = Database(tmp_path / "identity-alerts.sqlite")
    engine = AlertEngine()
    previous = state(stage="CONFIRMED")
    first_current = state(stage="MATURE")
    second_current = state(stage="MATURE")
    second_current["model_reference"]["candle_timestamp"] = "2026-01-08T00:00:00+00:00"
    first = engine.evaluate(previous, first_current, now=NOW)
    second = engine.evaluate(previous, second_current, now=NOW + timedelta(days=7))
    assert first and second and first[0]["event_identity"] != second[0]["event_identity"]
    assert database.save_alert(first[0]) is True
    assert database.save_alert(second[0]) is True
    assert len(database.list_alerts(limit=10)) == 2
    database.close()


def test_monitor_state_only_keeps_divergence_opposite_to_primary_direction():
    direction = {"instrument": "US Tech 100", "direction": "UP", "model_reference": {"timeframe": "1H", "candle_timestamp": "2026-01-01T00:00:00+00:00", "candle_state": "CLOSED"}}
    coordinated = {"timeframes": {"15M": {"candle_state": "CLOSED", "divergences": [
        {"pattern": "Bullish RSI Divergence", "lifecycle": "CONFIRMED", "start": "a"},
        {"pattern": "Bearish RSI Divergence", "lifecycle": "CONFIRMED", "start": "b"},
    ]}}}
    normalized = monitor_state(direction, coordinated)
    assert [item["pattern"] for item in normalized["early_warning"]] == ["Bearish RSI Divergence"]

    direction["direction"] = "DOWN"
    normalized = monitor_state(direction, coordinated)
    assert [item["pattern"] for item in normalized["early_warning"]] == ["Bullish RSI Divergence"]


def test_early_warning_and_pattern_confirmation_are_event_specific():
    engine = AlertEngine()
    previous = state()
    current = state(warnings=[{"pattern": "Bearish RSI Divergence", "instance_id": "div-1", "candle_state": "FORMING"}])
    current["trigger"]["candle_state"] = "FORMING"
    warning = next(item for item in engine.evaluate(previous, current, now=NOW) if item["alert_type"] == "EARLY_REVERSAL_WARNING")
    assert warning["state"] == "FORMING/PROVISIONAL"

    pattern_current = state(patterns={"15M:Doji:one": {"pattern": "Doji", "lifecycle": "CONFIRMED", "confirmed": False, "candle_state": "FORMING"}})
    pattern_alert = next(item for item in engine.evaluate(state(), pattern_current, now=NOW + timedelta(minutes=1)) if item["alert_type"] == "PATTERN_CONFIRMED")
    assert pattern_alert["state"] == "FORMING/PROVISIONAL"


def test_pattern_terminal_state_dedupes_but_new_instance_alerts():
    engine = AlertEngine()
    previous = state(patterns={"1H:Double Top:one": {"pattern": "Double Top", "lifecycle": "POTENTIAL", "confirmed": False}})
    confirmed = state(patterns={"1H:Double Top:one": {"pattern": "Double Top", "lifecycle": "CONFIRMED", "confirmed": True}})
    first = engine.evaluate(previous, confirmed, now=NOW)
    repeated = engine.evaluate(confirmed, confirmed, now=NOW + timedelta(minutes=1))
    new_instance = state(patterns={"1H:Double Top:two": {"pattern": "Double Top", "lifecycle": "CONFIRMED", "confirmed": True}})
    new_alerts = engine.evaluate(state(patterns={"1H:Double Top:two": {"pattern": "Double Top", "lifecycle": "POTENTIAL", "confirmed": False}}), new_instance, now=NOW + timedelta(minutes=2))
    assert any(item["alert_type"] == "PATTERN_CONFIRMED" for item in first)
    assert not repeated
    assert any(item["alert_type"] == "PATTERN_CONFIRMED" for item in new_alerts)


def test_engine_shaped_atr_context_emits_volatility_expansion():
    direction = {"instrument": "US Tech 100", "direction": "UP", "trend_stage": "CONFIRMED", "reversal_risk": {"category": "LOW"}, "model_reference": {"timeframe": "1H", "candle_timestamp": "2026-01-01T00:00:00+00:00", "candle_state": "CLOSED"}}
    coordinated = {"timeframes": {"1H": {"candle_state": "CLOSED", "context": {"atr": {"recent_comparison": "higher_than_recent"}}}}}
    current = monitor_state(direction, coordinated)
    previous = dict(current)
    previous["volatility"] = {"1H": {"comparison": "lower_than_recent", "candle_state": "CLOSED"}}
    alerts = AlertEngine().evaluate(previous, current, now=NOW)
    assert any(item["alert_type"] == "VOLATILITY_EXPANSION" and item["confirmed"] for item in alerts)


def test_score_threshold_requires_structural_support():
    engine = AlertEngine(AlertConfig(score_change_threshold=5))
    previous = state(score=70, structures={"1H": {"breakout": None, "break_of_structure": False, "false_breakout": False}})
    below = state(score=74, structures={"1H": {"breakout": "up", "break_of_structure": False, "false_breakout": False}})
    assert not any(item["alert_type"] == "DIRECTION_SHIFT" for item in engine.evaluate(previous, below, now=NOW))
    above = state(score=76, structures={"1H": {"breakout": "up", "break_of_structure": False, "false_breakout": False}})
    assert any(item["alert_type"] == "DIRECTION_SHIFT" for item in engine.evaluate(previous, above, now=NOW + timedelta(minutes=1)))


def test_atomic_monitor_evaluation_rolls_back_state_and_alerts(tmp_path):
    database = Database(tmp_path / "atomic-alerts.sqlite")
    engine = AlertEngine()
    current = state(stage="MATURE")
    alerts = engine.evaluate(state(), current, now=NOW)
    invalid = {"alert_id": "invalid"}
    with pytest.raises(KeyError):
        database.save_monitor_evaluation(current, [alerts[0], invalid])
    assert database.get_monitor_state("US Tech 100") is None
    assert database.list_alerts(limit=10) == []
    database.close()


def test_first_state_for_all_three_markets_has_no_startup_alert(tmp_path):
    database = Database(tmp_path / "first-state.sqlite")
    for instrument in ("US Tech 100", "Japan 225", "Hong Kong HS50"):
        current = state()
        current["instrument"] = instrument
        database.save_monitor_evaluation(current, AlertEngine().evaluate(None, current, now=NOW))
    assert database.list_alerts(limit=10) == []
    assert len(database.get_monitor_status()["states"]) == 3
    database.close()


def test_monitor_cli_is_exposed_with_duration_in_seconds():
    result = subprocess.run(["ig-ai", "--help"], capture_output=True, text=True, check=True)
    assert "monitor" in result.stdout
