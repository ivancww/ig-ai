from ig_ai.database import Database
from ig_ai.direction import DIRECTION_SCHEMA_VERSION, DirectionScoreEngine, DirectionWeights


def structure(direction="UP", *, breakout=None, false_breakout=False, labels=None):
    labels = labels or (["HH", "HL"] if direction == "UP" else ["LH", "LL"] if direction == "DOWN" else [])
    highs = [{"classification": label} for label in labels if label in {"HH", "LH"}]
    lows = [{"classification": label} for label in labels if label in {"HL", "LL"}]
    return {"direction": f"{direction}_STRUCTURE" if direction != "NEUTRAL" else "RANGE", "confirmed_highs": highs, "confirmed_lows": lows, "breakout": breakout, "break_of_structure": breakout is not None, "false_breakout": false_breakout}


def analysis(timeframe, direction="UP", *, state="CLOSED", divergence=None, macd_state="strengthening", breakout=None, false_breakout=False, labels=None):
    return {
        "instrument": "TEST.EPIC", "timeframe": timeframe, "candle_timestamp": "2025-01-01T00:00:00+00:00", "candle_state": state,
        "structure": structure(direction, breakout=breakout, false_breakout=false_breakout, labels=labels),
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
    result = DirectionScoreEngine().analyze(all_timeframes("DOWN", breakout="down"))
    assert result["trend_stage"] == "REVERSAL_CONFIRMED"
    forming = all_timeframes("DOWN", breakout="down", state="FORMING")
    assert DirectionScoreEngine().analyze(forming)["trend_stage"] != "REVERSAL_CONFIRMED"


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
    assert result["up_score"] + result["down_score"] == 100


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


def test_schema_and_no_probability_or_recommendation_fields_are_safe(tmp_path):
    database = Database(tmp_path / "direction.sqlite")
    tables = {row[0] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"direction_snapshots", "direction_outcomes"} <= tables
    result = DirectionScoreEngine().analyze(all_timeframes())
    assert result["recommendation"] is None
    assert "probability" not in result
    database.close()
