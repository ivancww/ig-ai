from datetime import UTC, datetime
from decimal import Decimal

from ig_ai.cli import direction_evidence_sections
from ig_ai.database import Database
from ig_ai.direction import DIRECTION_SCHEMA_VERSION, DirectionScoreEngine, DirectionWeights
from ig_ai.models import Candle, Instrument
from ig_ai.runtime import PersistedStream


def structure(direction="UP", *, breakout=None, false_breakout=False, labels=None, prior_direction=None, bos=False):
    labels = labels or (["HH", "HL"] if direction == "UP" else ["LH", "LL"] if direction == "DOWN" else [])
    highs = [{"classification": label} for label in labels if label in {"HH", "LH"}]
    lows = [{"classification": label} for label in labels if label in {"HL", "LL"}]
    return {"direction": f"{direction}_STRUCTURE" if direction != "NEUTRAL" else "RANGE", "current_direction": direction, "prior_direction": prior_direction or "NEUTRAL", "confirmed_highs": highs, "confirmed_lows": lows, "breakout": breakout, "breakout_direction": breakout, "break_of_structure": bos, "false_breakout": false_breakout, "false_breakout_direction": direction if false_breakout else None}


def analysis(timeframe, direction="UP", *, state="CLOSED", divergence=None, macd_state="strengthening", breakout=None, false_breakout=False, labels=None, prior_direction=None, bos=False):
    return {
        "instrument": "TEST.EPIC", "timeframe": timeframe, "candle_timestamp": "2025-01-01T00:00:00+00:00", "candle_state": state,
        "structure": structure(direction, breakout=breakout, false_breakout=false_breakout, labels=labels, prior_direction=prior_direction, bos=bos),
        "context": {"moving_averages": {"ema20": {"price_relation": "above"}, "ema50": {"price_relation": "above"}}, "ema_ordering": {"ema10_gt_ema20": True, "ema20_gt_ema50": True, "ema50_gt_ema100": True, "ema100_gt_ema200": True}, "macd": {"polarity": "positive" if direction == "UP" else "negative", "relative_to_signal": "above" if direction == "UP" else "below", "histogram_state": macd_state}, "rsi14": {"value": 62 if direction == "UP" else 38}, "atr14": {"recent_comparison": "lower_than_recent"}},
        "divergences": divergence or [], "candlestick_patterns": [], "chart_patterns": [],
    }


def all_timeframes(primary="UP", **kwargs):
    return {timeframe: analysis(timeframe, primary, **kwargs) for timeframe in ("15M", "1H", "4H", "1D")}


def test_bullish_aligned_trend_has_score_and_agreement():
    result = DirectionScoreEngine().analyze(all_timeframes())
    assert result["direction"] == "UP"
    assert result["up_score"] > result["down_score"]
    assert result["timeframe_agreement"]["status"] == "AGREEMENT"
    assert result["technical_score_only"] is True
    assert result["calibrated_probability"] is None


def test_bearish_aligned_trend_and_high_risk_remain_distinct():
    result = DirectionScoreEngine().analyze({timeframe: analysis(timeframe, "DOWN", macd_state="weakening", false_breakout=True, labels=["LH"]) for timeframe in ("15M", "1H", "4H", "1D")})
    assert result["direction"] == "DOWN"
    assert result["trend_stage"] == "EXHAUSTION_RISK"
    assert result["reversal_risk"]["category"] in {"HIGH", "VERY_HIGH"}


def test_bullish_direction_can_have_high_reversal_risk():
    result = DirectionScoreEngine().analyze(all_timeframes(macd_state="weakening", false_breakout=True))
    assert result["direction"] == "UP"
    assert result["reversal_risk"]["category"] == "HIGH"


def test_15m_warning_does_not_reverse_1h_bullish_direction():
    histories = all_timeframes()
    histories["15M"] = analysis("15M", "DOWN", divergence=[{"pattern": "Bearish RSI Divergence", "lifecycle": "CONFIRMED"}])
    result = DirectionScoreEngine().analyze(histories)
    assert result["direction"] == "UP"
    assert result["trend_stage"] in {"MATURE", "EXHAUSTION_RISK"}
    assert result["reversal_risk"]["category"] != "LOW"


def test_15m_warning_while_1h_bearish_keeps_bearish_direction():
    histories = all_timeframes("DOWN")
    histories["15M"] = analysis("15M", "UP", divergence=[{"pattern": "Bullish MACD Divergence", "lifecycle": "CONFIRMED"}])
    result = DirectionScoreEngine().analyze(histories)
    assert result["direction"] == "DOWN"
    assert result["timeframe_agreement"]["status"] == "PARTIAL_AGREEMENT"


def test_primary_break_and_lh_ll_confirm_only_when_closed():
    result = DirectionScoreEngine().analyze({timeframe: analysis(timeframe, "DOWN", breakout="down", prior_direction="UP", bos=True) for timeframe in ("15M", "1H", "4H", "1D")})
    assert result["trend_stage"] == "REVERSAL_CONFIRMED"
    forming = {timeframe: analysis(timeframe, "DOWN", breakout="down", prior_direction="UP", bos=True, state="FORMING") for timeframe in ("15M", "1H", "4H", "1D")}
    assert DirectionScoreEngine().analyze(forming)["trend_stage"] != "REVERSAL_CONFIRMED"


def test_reversal_direction_is_explicit_and_continuation_is_not_reversal():
    engine = DirectionScoreEngine()
    up_to_down = {timeframe: analysis(timeframe, "DOWN", prior_direction="UP", bos=True) for timeframe in ("15M", "1H", "4H", "1D")}
    down_to_up = {timeframe: analysis(timeframe, "UP", prior_direction="DOWN", bos=True) for timeframe in ("15M", "1H", "4H", "1D")}
    bearish_continuation = all_timeframes("DOWN")
    bullish_continuation = all_timeframes("UP")
    assert engine.analyze(up_to_down)["trend_stage"] == "REVERSAL_CONFIRMED"
    assert engine.analyze(down_to_up)["trend_stage"] == "REVERSAL_CONFIRMED"
    assert engine.analyze(bearish_continuation)["trend_stage"] != "REVERSAL_CONFIRMED"
    assert engine.analyze(bullish_continuation)["trend_stage"] != "REVERSAL_CONFIRMED"


def test_breakout_false_breakout_and_bos_are_distinct():
    continuation = analysis("1H", "UP", breakout="up", prior_direction="UP", bos=False)
    transition = analysis("1H", "DOWN", breakout="down", prior_direction="UP", bos=True)
    failed = analysis("1H", "UP", false_breakout=True, prior_direction="UP", bos=False)
    assert continuation["structure"]["breakout"] == "up"
    assert continuation["structure"]["break_of_structure"] is False
    assert transition["structure"]["break_of_structure"] is True
    assert failed["structure"]["false_breakout"] is True
    assert failed["structure"]["break_of_structure"] is False


def test_cli_evidence_sections_are_relative_to_bearish_primary_direction():
    histories = all_timeframes("DOWN")
    histories["15M"] = analysis("15M", "UP")
    snapshot = DirectionScoreEngine().analyze(histories)
    supporting, opposing = direction_evidence_sections(snapshot)
    assert supporting and all(item["direction"] == "DOWN" for item in supporting)
    assert opposing and all(item["direction"] == "UP" for item in opposing)


def test_higher_timeframe_and_daily_conflicts_are_visible():
    histories = all_timeframes()
    histories["4H"] = analysis("4H", "DOWN")
    histories["1D"] = analysis("1D", "DOWN")
    result = DirectionScoreEngine().analyze(histories)
    assert result["timeframe_agreement"]["status"] == "HIGH_CONFLICT"
    assert set(result["timeframe_agreement"]["contradictory_timeframes"]) == {"4H", "1D"}


def test_macd_weakening_and_failed_breakout_are_ledger_evidence():
    result = DirectionScoreEngine().analyze(all_timeframes(macd_state="weakening", false_breakout=True))
    types = {item["type"] for item in result["reversal_risk"]["evidence"]}
    assert {"1H_MACD_WEAKENING", "1H_FAILED_BREAKOUT"} <= types
    assert any(item["evidence_type"] == "MACD" for item in result["evidence_ledger"])


def test_holding_window_shortens_as_trend_matures():
    engine = DirectionScoreEngine()
    confirmed = engine.analyze(all_timeframes())
    mature = engine.analyze(all_timeframes(macd_state="weakening"))
    watch = engine.analyze(all_timeframes(macd_state="weakening", false_breakout=True, breakout="up"))
    assert confirmed["holding_window"] == "4–8H"
    assert mature["holding_window"] in {"2–4H", "1–2H"}
    assert watch["holding_window"] in {"1–2H", "15–60M"}


def test_weight_versioning_and_score_normalization():
    weights = DirectionWeights(version="direction_score_test", primary_structure=100)
    result = DirectionScoreEngine(weights).analyze(all_timeframes())
    assert result["score_version"] == "direction_score_test"
    assert 0 <= result["up_score"] <= 100 and 0 <= result["down_score"] <= 100
    assert result["up_score"] + result["down_score"] <= 100
    assert 0 < result["coverage"]["ratio"] <= 1


def test_sparse_evidence_has_lower_coverage_than_broad_agreement():
    sparse = {"1H": analysis("1H", "UP")}
    broad = all_timeframes("UP")
    sparse_result = DirectionScoreEngine().analyze(sparse)
    broad_result = DirectionScoreEngine().analyze(broad)
    assert sparse_result["coverage"]["ratio"] < broad_result["coverage"]["ratio"]
    assert sparse_result["up_score"] < broad_result["up_score"]


def test_forming_higher_timeframes_are_provisional_and_discounted():
    closed = all_timeframes("UP")
    forming = all_timeframes("UP")
    forming["4H"]["candle_state"] = "FORMING"
    forming["1D"]["candle_state"] = "FORMING"
    closed_result = DirectionScoreEngine().analyze(closed)
    forming_result = DirectionScoreEngine().analyze(forming)
    ledger = {item["timeframe"]: item for item in forming_result["evidence_ledger"] if item["evidence_type"] in {"4H_CONTEXT", "1D_CONTEXT"}}
    assert all(item["provisional"] and item["applied_weight"] < item["weight"] for item in ledger.values())
    assert forming_result["up_score"] < closed_result["up_score"]


def test_forming_15m_can_warn_without_full_confirmed_weight():
    histories = all_timeframes("UP")
    histories["15M"] = analysis("15M", "DOWN", state="FORMING", divergence=[{"pattern": "Bearish RSI Divergence", "lifecycle": "FORMING"}])
    result = DirectionScoreEngine().analyze(histories)
    item = next(item for item in result["evidence_ledger"] if item["evidence_type"] == "15M_CONFIRMATION")
    assert result["trend_stage"] != "REVERSAL_CONFIRMED"
    assert item["provisional"] and item["applied_weight"] < item["weight"]


def test_risk_evidence_is_relative_to_both_primary_directions():
    engine = DirectionScoreEngine()
    up = engine.analyze(all_timeframes("UP", macd_state="weakening", false_breakout=True))
    down = engine.analyze({timeframe: analysis(timeframe, "DOWN", macd_state="weakening", false_breakout=True, labels=["LH"], prior_direction="UP") for timeframe in ("15M", "1H", "4H", "1D")})
    assert {item["type"] for item in up["reversal_risk"]["evidence"]} == {"1H_MACD_WEAKENING", "1H_FAILED_BREAKOUT"}
    assert {item["type"] for item in down["reversal_risk"]["evidence"]} == {"1H_MACD_WEAKENING", "1H_FAILED_BREAKOUT"}


def test_persistence_is_idempotent_and_future_outcomes_stay_null(tmp_path):
    database = Database(tmp_path / "direction.sqlite")
    snapshot = DirectionScoreEngine().analyze(all_timeframes())
    database.save_direction_snapshot(snapshot)
    database.save_direction_snapshot(snapshot)
    row = database.get_direction_status("TEST.EPIC", "1H")
    assert row["schema_version"] == DIRECTION_SCHEMA_VERSION
    assert database.connection.execute("SELECT COUNT(*) FROM direction_snapshots").fetchone()[0] == 1
    database.close()
    database = Database(tmp_path / "direction.sqlite")
    assert database.get_direction_status("TEST.EPIC", "1H")["score_version"] == snapshot["score_version"]
    database.save_direction_outcome(instrument_id="TEST.EPIC", timeframe="1H", snapshot_candle_start=snapshot["candle_timestamp"], horizon="4H", reference_price="100")
    outcome = database.connection.execute("SELECT future_price, percentage_move FROM direction_outcomes").fetchone()
    assert outcome == (None, None)
    database.close()


def test_runtime_persists_primary_1h_model_with_separate_trigger_metadata(tmp_path):
    database = Database(tmp_path / "runtime-direction.sqlite")
    sink = PersistedStream(database, {"TEST": Instrument("TEST", "TEST.EPIC", "Test")})
    primary = analysis("1H", "UP")
    score = DirectionScoreEngine().analyze({timeframe: analysis(timeframe, "UP") for timeframe in ("15M", "1H", "4H", "1D")})
    sink.mtf_coordinator.analyze = lambda histories, **kwargs: {"timeframes": {"1H": primary, "15M": analysis("15M", "UP", state="FORMING")}, "direction_reversal": {}, "direction_score": score}
    trigger = Candle("TEST", "TEST.EPIC", "15M", datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, 0, 15, tzinfo=UTC), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), is_closed=False, observation_count=1)
    sink._save_phase2b_for_candle(trigger)
    row = database.connection.execute("SELECT timeframe, candle_start, snapshot_json FROM direction_snapshots").fetchone()
    assert row[0] == "1H"
    assert row[1] == score["candle_timestamp"]
    assert '"trigger":{"candle_state":"FORMING","candle_timestamp":"2025-01-01T00:00:00+00:00","timeframe":"15M"}' in row[2]
    database.close()


def test_schema_and_no_probability_or_recommendation_fields_are_safe(tmp_path):
    database = Database(tmp_path / "direction.sqlite")
    tables = {row[0] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"direction_snapshots", "direction_outcomes"} <= tables
    result = DirectionScoreEngine().analyze(all_timeframes())
    assert result["recommendation"] is None
    assert "probability" not in result
    database.close()
