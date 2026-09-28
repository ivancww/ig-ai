import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ig_ai.database import Database
from ig_ai.models import Candle, Instrument
from ig_ai.patterns import (
    CANDLESTICK_PATTERN_NAMES,
    CHART_PATTERN_NAMES,
    CandlestickPatternEngine,
    ChartPatternEngine,
    DirectionReversalEngine,
    MarketStructureEngine,
    MultiTimeframeCoordinator,
    Phase2BEngine,
    lifecycle_transition,
)
from ig_ai.runtime import PersistedStream


def make_candle(index: int, *, open_: float, high: float, low: float, close: float, closed: bool = True, timeframe: str = "1H", observation_count: int = 1) -> Candle:
    start = datetime(2025, 2, 1, tzinfo=UTC) + timedelta(hours=index)
    duration = {"15M": timedelta(minutes=15), "1H": timedelta(hours=1), "4H": timedelta(hours=4), "1D": timedelta(days=1)}[timeframe]
    return Candle("TEST", "TEST.EPIC", timeframe, start, start + duration, Decimal(str(open_)), Decimal(str(high)), Decimal(str(low)), Decimal(str(close)), is_closed=closed, observation_count=observation_count)


def test_candlestick_geometry_and_catalog():
    doji = make_candle(0, open_=10, high=12, low=9, close=10.05)
    patterns = {item["pattern"] for item in CandlestickPatternEngine().detect([doji])}
    assert "Doji" in patterns
    assert "Long Upper Wick" in patterns
    assert set(CANDLESTICK_PATTERN_NAMES) >= {"Morning Star", "Three Black Crows", "Bullish Engulfing"}


def candlestick_fixture(name):
    if name == "Doji":
        return [make_candle(0, open_=10, high=11, low=9, close=10.05)]
    if name in {"Hammer", "Inverted Hammer", "Hanging Man", "Shooting Star"}:
        values = [13, 12, 11] if name in {"Hammer", "Inverted Hammer"} else [10, 11, 12]
        candles = [make_candle(i, open_=value, high=value + 0.2, low=value - 0.2, close=value - 1 if name in {"Hammer", "Inverted Hammer"} else value + 1) for i, value in enumerate(values)]
        if name in {"Hammer", "Hanging Man"}:
            candles.append(make_candle(3, open_=10, high=10.05, low=7, close=10.2))
        else:
            candles.append(make_candle(3, open_=10, high=13, low=9.95, close=10.2))
        return candles
    if name == "Spinning Top":
        return [make_candle(0, open_=10, high=11, low=9, close=10.4)]
    if name == "Marubozu":
        return [make_candle(0, open_=10, high=12, low=10, close=12)]
    if name == "Long Upper Wick":
        return [make_candle(0, open_=10, high=14, low=9.9, close=10.1)]
    if name == "Long Lower Wick":
        return [make_candle(0, open_=10, high=10.1, low=6, close=10.05)]
    if name == "Inside Bar":
        return [make_candle(0, open_=10, high=12, low=8, close=11), make_candle(1, open_=10, high=11, low=9, close=10.5)]
    if name == "Outside Bar":
        return [make_candle(0, open_=10, high=11, low=9, close=10.5), make_candle(1, open_=9, high=12, low=8, close=11)]
    if name == "Bullish Engulfing":
        return [make_candle(0, open_=11, high=11.5, low=9.5, close=10), make_candle(1, open_=9.8, high=12.5, low=9.5, close=12)]
    if name == "Bearish Engulfing":
        return [make_candle(0, open_=10, high=11.5, low=9.5, close=11), make_candle(1, open_=11.2, high=12, low=9, close=9.5)]
    if name == "Bullish Harami":
        return [make_candle(0, open_=12, high=12.5, low=9.5, close=10), make_candle(1, open_=10.5, high=11.5, low=10, close=11)]
    if name == "Bearish Harami":
        return [make_candle(0, open_=10, high=12.5, low=9.5, close=12), make_candle(1, open_=11.5, high=12, low=10.5, close=11)]
    if name == "Piercing Line":
        return [make_candle(0, open_=12, high=12.5, low=9, close=10), make_candle(1, open_=8.5, high=11.5, low=8, close=11.5)]
    if name == "Dark Cloud Cover":
        return [make_candle(0, open_=10, high=13, low=9.5, close=12), make_candle(1, open_=13.5, high=14, low=10, close=10.5)]
    if name == "Morning Star":
        return [make_candle(0, open_=12, high=12.5, low=9.5, close=10), make_candle(1, open_=9.8, high=10, low=9.6, close=9.9), make_candle(2, open_=10, high=12, low=9.8, close=11.5)]
    if name == "Evening Star":
        return [make_candle(0, open_=10, high=12.5, low=9.5, close=12), make_candle(1, open_=12, high=12.2, low=11.8, close=12.1), make_candle(2, open_=12, high=12.2, low=9, close=10.5)]
    if name == "Tweezer Bottom":
        return [make_candle(0, open_=11, high=11.5, low=9, close=10), make_candle(1, open_=10, high=11.5, low=9, close=11)]
    if name == "Tweezer Top":
        return [make_candle(0, open_=10, high=12, low=9, close=11), make_candle(1, open_=11, high=12, low=9.5, close=10)]
    if name == "Three White Soldiers":
        return [make_candle(i, open_=10 + i, high=11.2 + i, low=9.9 + i, close=11 + i) for i in range(3)]
    if name == "Three Black Crows":
        return [make_candle(i, open_=13 - i, high=13.1 - i, low=11.8 - i, close=12 - i) for i in range(3)]
    raise AssertionError(name)


@pytest.mark.parametrize("name", CANDLESTICK_PATTERN_NAMES)
def test_every_advertised_candlestick_detector_has_positive_fixture(name):
    detected = {item["pattern"] for item in CandlestickPatternEngine().detect(candlestick_fixture(name))}
    assert name in detected


def test_multi_candle_patterns_are_deterministic():
    candles = [
        make_candle(0, open_=11, high=11.5, low=9.5, close=10),
        make_candle(1, open_=9.8, high=12.5, low=9.5, close=12),
    ]
    patterns = {item["pattern"] for item in CandlestickPatternEngine().detect(candles)}
    assert "Bullish Engulfing" in patterns
    assert "Outside Bar" in patterns


def test_forming_pattern_is_not_final():
    forming = make_candle(0, open_=10, high=11, low=8, close=10.2, closed=False)
    result = CandlestickPatternEngine().detect([forming])
    assert result and all(item["lifecycle"] == "FORMING" for item in result)


def test_context_names_require_prior_trend_and_do_not_overlabel_identical_geometry():
    def geometry(i, values):
        return make_candle(i, open_=values, high=values + 0.05, low=values - 3, close=values + 0.2)
    downtrend = [make_candle(0, open_=13, high=13.5, low=12.5, close=12), make_candle(1, open_=12, high=12.5, low=11.5, close=11), make_candle(2, open_=11, high=11.5, low=10.5, close=10), geometry(3, 10)]
    uptrend = [make_candle(0, open_=10, high=10.5, low=9.5, close=11), make_candle(1, open_=11, high=11.5, low=10.5, close=12), make_candle(2, open_=12, high=12.5, low=11.5, close=13), geometry(3, 13)]
    down_names = {item["pattern"] for item in CandlestickPatternEngine().detect(downtrend)}
    up_names = {item["pattern"] for item in CandlestickPatternEngine().detect(uptrend)}
    assert "Hammer" in down_names and "Hanging Man" not in down_names
    assert "Hanging Man" in up_names and "Hammer" not in up_names


def test_island_reversal_requires_opposite_closed_gaps():
    valid = [make_candle(0, open_=10, high=10, low=9, close=9.5), make_candle(1, open_=12.5, high=13, low=12, close=12.5), make_candle(2, open_=9.5, high=10, low=8, close=9)]
    same_direction = [valid[0], valid[1], make_candle(2, open_=14, high=15, low=14, close=14.5)]
    assert any(item["pattern"] == "Island Reversal" for item in ChartPatternEngine().detect(valid))
    assert not any(item["pattern"] == "Island Reversal" for item in ChartPatternEngine().detect(same_direction))


def test_chart_pattern_confirmation_and_invalidation_levels_are_distinct_and_bounded():
    candles = [
        make_candle(0, open_=10, high=10, low=9, close=10),
        make_candle(1, open_=11, high=12, low=10, close=11),
        make_candle(2, open_=9, high=11, low=8, close=9),
        make_candle(3, open_=11, high=12.1, low=10, close=11),
        make_candle(4, open_=10, high=11, low=9, close=10),
    ]
    double_top = next(item for item in ChartPatternEngine().detect(candles) if item["pattern"] == "Double Top")
    assert double_top["confirmation_level"] == pytest.approx(8)
    assert double_top["invalidation_level"] == pytest.approx(12.1)
    assert double_top["confirmation_level"] != double_top["invalidation_level"]
    assert double_top["lifecycle"] in {"POTENTIAL", "NEAR_CONFIRMATION", "CONFIRMED", "FAILED"}


def test_engine_driven_double_top_lifecycle_keeps_instance_id_and_reaches_terminal_state():
    candles = [
        make_candle(0, open_=10, high=10, low=9, close=10),
        make_candle(1, open_=11, high=12, low=10, close=11),
        make_candle(2, open_=9, high=11, low=8, close=9),
        make_candle(3, open_=11, high=12.1, low=10, close=11),
        make_candle(4, open_=10, high=11, low=9, close=10),
    ]
    engine = ChartPatternEngine()
    potential = next(item for item in engine.detect(candles) if item["pattern"] == "Double Top")
    confirmed = next(item for item in engine.detect(candles + [make_candle(5, open_=8, high=9, low=7, close=7)]) if item["pattern"] == "Double Top")
    failed = next(item for item in engine.detect(candles + [make_candle(5, open_=13, high=14, low=12, close=13)]) if item["pattern"] == "Double Top")
    assert potential["instance_id"] == confirmed["instance_id"] == failed["instance_id"]
    assert confirmed["lifecycle"] == "CONFIRMED"
    assert failed["lifecycle"] == "FAILED"


def test_lifecycle_transition_and_chart_catalog():
    assert lifecycle_transition("FORMING") == "POTENTIAL"
    assert lifecycle_transition("POTENTIAL") == "NEAR_CONFIRMATION"
    assert lifecycle_transition("NEAR_CONFIRMATION", confirmed=True) == "CONFIRMED"
    assert lifecycle_transition("CONFIRMED", invalidated=True) == "FAILED"
    assert "Island Reversal" in CHART_PATTERN_NAMES
    assert "Cup & Handle" in ChartPatternEngine.supported_patterns()


def chart_swing_fixture(high_values, low_values):
    candles = [make_candle(0, open_=low_values[0] + 1, high=low_values[0] + 2, low=low_values[0], close=low_values[0] + 1)]
    for index, (high, low) in enumerate(zip(high_values, low_values, strict=True), start=1):
        candles.append(make_candle(index * 2 - 1, open_=high - 1, high=high, low=high - 2, close=high - 1))
        candles.append(make_candle(index * 2, open_=low + 1, high=low + 2, low=low, close=low + 1))
    return candles


CHART_POSITIVE_FIXTURES = {
    "Double Top": ([10, 12, 12, 12], [8, 10, 8, 10]),
    "Double Bottom": ([12, 10, 12, 10], [10, 8, 8, 10]),
    "Triple Top": ([10, 12, 12, 12], [8, 9, 8, 9]),
    "Triple Bottom": ([12, 14, 12, 14, 12, 14, 12], [10, 8, 10, 8, 10, 8, 10]),
    "Head & Shoulders": ([10, 12, 14, 12], [8, 9, 8, 9]),
    "Inverse Head & Shoulders": ([14, 12, 14, 12, 14, 12], [12, 10, 7, 10, 12, 11]),
    "Triangle": ([14, 14, 13, 12], [8, 8, 9, 10]),
    "Rising Wedge": ([12, 12, 13, 14], [8, 8, 9, 10]),
    "Falling Wedge": ([14, 14, 13, 10], [10, 10, 9, 8]),
    "Flag": ([15, 14, 13, 12, 11], [11, 10, 9, 8, 7]),
    "Pennant": ([14, 13, 14, 12, 14], [10, 9, 8, 9, 10]),
    "Rectangle": ([12, 12, 12, 12], [8, 8, 8, 8]),
    "Cup & Handle": ([14, 14, 13, 14, 14], [10, 10, 7, 10, 10]),
    "Rounded Top": ([14, 14, 12, 14], [8, 8, 9, 8]),
    "Rounded Bottom": ([14, 13, 14, 13, 14], [10, 8, 10, 8, 10]),
}


@pytest.mark.parametrize("name", tuple(CHART_POSITIVE_FIXTURES))
def test_every_advertised_chart_detector_has_positive_fixture(name):
    highs, lows = CHART_POSITIVE_FIXTURES[name]
    detected = {item["pattern"] for item in ChartPatternEngine().detect(chart_swing_fixture(highs, lows))}
    assert name in detected


def test_structure_exposes_hh_hl_lh_ll_without_lookahead():
    closes = [10, 13, 9, 12, 8, 14, 7]
    candles = [make_candle(i, open_=value, high=value + 0.5, low=value - 0.5, close=value) for i, value in enumerate(closes)]
    state = MarketStructureEngine().analyze(candles)
    assert state["confirmed_highs"]
    assert state["confirmed_lows"]
    assert state["breakout"] in {None, "up", "down"}
    forming = candles[:-1] + [make_candle(6, open_=7, high=20, low=6, close=7, closed=False)]
    assert len(MarketStructureEngine().analyze(forming)["confirmed_highs"]) <= len(state["confirmed_highs"])


def test_structure_distinguishes_breakout_retest_and_false_breakout():
    base = [
        make_candle(0, open_=10, high=10, low=9, close=10),
        make_candle(1, open_=11, high=12, low=10, close=11),
        make_candle(2, open_=10, high=11, low=9, close=10),
    ]
    false_break = base + [make_candle(3, open_=11, high=13, low=10, close=11.5)]
    breakout = base + [make_candle(3, open_=12, high=13, low=11, close=13)]
    retest = base + [make_candle(3, open_=12, high=13, low=11, close=13), make_candle(4, open_=12.5, high=14, low=11.5, close=12.5)]
    assert MarketStructureEngine().analyze(false_break)["false_breakout"] is True
    assert MarketStructureEngine().analyze(breakout)["breakout"] == "up"
    assert MarketStructureEngine().analyze(retest)["retest"] is True


def test_phase2b_context_includes_technical_evidence_and_divergence_is_bounded():
    candles = [make_candle(i, open_=100 + i, high=102 + i, low=98 + i, close=100 + i) for i in range(40)]
    engine = Phase2BEngine()
    earlier = engine.analyze(candles, target_index=35)
    later = engine.analyze(candles + [make_candle(40, open_=500, high=501, low=499, close=500)], target_index=35)
    assert earlier["context"].keys() >= {"rsi", "macd", "atr", "gaps", "support_resistance"}
    assert earlier["divergences"] == later["divergences"]
    assert earlier["direction_reversal"] is not None


def test_direction_engine_keeps_15m_warning_distinct_from_1h_state():
    one_hour = {"structure": {"confirmed_highs": [{"classification": "HH"}], "confirmed_lows": [{"classification": "HL"}]}, "macd": {"histogram_state": "unchanged"}}
    result = DirectionReversalEngine().classify({"1H": one_hour}, {"15M": [{"pattern": "Bullish RSI Divergence"}]})
    assert result["direction_structure"] == "UP_STRUCTURE"
    assert result["state"] == "EARLY"
    assert result["probability"] is None
    assert result["recommendation"] is None


def test_direction_state_precedence_covers_all_evidence_states():
    base = {"structure": {"confirmed_highs": [{"classification": "HH"}], "confirmed_lows": [{"classification": "HL"}], "break_of_structure": False}, "macd": {"histogram_state": "unchanged"}}
    engine = DirectionReversalEngine()
    assert engine.classify({"1H": base})["state"] == "CONFIRMED"
    assert engine.classify({"1H": base}, {"15M": [{"pattern": "Bullish RSI Divergence"}]})["state"] == "EARLY"
    mature = {**base, "macd": {"histogram_state": "weakening"}}
    assert engine.classify({"1H": mature})["state"] == "MATURE"
    assert engine.classify({"1H": mature}, {"15M": [{"pattern": "Bullish RSI Divergence"}]})["state"] == "EXHAUSTION_RISK"
    watch = {**base, "structure": {**base["structure"], "break_of_structure": True}}
    assert engine.classify({"1H": watch})["state"] == "REVERSAL_WATCH"
    confirmed = {"structure": {"confirmed_highs": [{"classification": "LH"}], "confirmed_lows": [{"classification": "LL"}], "break_of_structure": True}, "macd": {"histogram_state": "weakening"}}
    assert engine.classify({"1H": confirmed})["state"] == "REVERSAL_CONFIRMED"


def test_multi_timeframe_coordinator_keeps_real_timeframes_distinct_and_bounded():
    histories = {
        timeframe: [make_candle(i, open_=100 + i, high=102 + i, low=98 + i, close=100 + i, timeframe=timeframe) for i in range(20)]
        for timeframe in ("15M", "1H", "4H", "1D")
    }
    result = MultiTimeframeCoordinator(max_history=7).analyze(histories)
    assert set(result["timeframes"]) == {"15M", "1H", "4H", "1D"}
    assert result["history_lengths"] == {"15M": 7, "1H": 7, "4H": 7, "1D": 7}
    assert result["direction_reversal"]["timeframe_roles"]["1H"] == "PRIMARY_DIRECTION"
    assert set(result["direction_reversal"]["higher_timeframe_context"]) == {"4H", "1D"}


def test_multi_timeframe_alignment_uses_closed_end_not_start_only():
    histories = {
        "15M": [make_candle(i, open_=100, high=102, low=98, close=100, timeframe="15M") for i in range(8)],
        "1H": [make_candle(i, open_=100, high=102, low=98, close=100, timeframe="1H") for i in range(6)],
        "4H": [make_candle(i * 4, open_=100, high=102, low=98, close=100, timeframe="4H") for i in range(3)],
        "1D": [make_candle(i * 24, open_=100, high=102, low=98, close=100, timeframe="1D") for i in range(2)],
    }
    target = histories["1H"][4]
    histories["4H"][1] = make_candle(4, open_=100, high=200, low=99, close=150, timeframe="4H", closed=False)
    result = MultiTimeframeCoordinator(max_history=20).analyze(histories, target_time=target.start, target_timeframe="1H")
    assert result["history_lengths"]["4H"] == 1
    assert result["history_lengths"]["1D"] == 0


def test_runtime_throttles_forming_phase2b_analysis(tmp_path):
    database = Database(tmp_path / "throttle.sqlite")
    sink = PersistedStream(database, {"TEST": Instrument("TEST", "TEST.EPIC", "Test")})
    for count in range(1, 11):
        sink._save_phase2b_for_candle(make_candle(count, open_=100, high=101, low=99, close=100, closed=False, observation_count=count))
    assert sink.phase2b_runs == 2
    assert sink.phase2b_skipped_forming == 8
    database.close()


def test_phase2b_persistence_is_idempotent_and_restart_safe(tmp_path):
    database = Database(tmp_path / "phase2b.sqlite")
    candles = [make_candle(i, open_=100 + i, high=102 + i, low=98 + i, close=100 + i) for i in range(40)]
    analysis = Phase2BEngine().analyze(candles)
    database.save_phase2b_analysis(analysis)
    database.save_phase2b_analysis(analysis)
    assert database.connection.execute("SELECT COUNT(*) FROM structure_states").fetchone()[0] == 1
    evolving = {**analysis, "candle_timestamp": candles[-2].start.isoformat(), "chart_patterns": [{"pattern": "Double Top", "instance_id": "Double Top:stable", "start": candles[-3].start.isoformat(), "end": candles[-2].end.isoformat(), "lifecycle": "POTENTIAL", "candle_state": "CLOSED"}]}
    database.save_phase2b_analysis(evolving)
    evolving["candle_timestamp"] = candles[-1].start.isoformat()
    evolving["chart_patterns"][0]["lifecycle"] = "FAILED"
    database.save_phase2b_analysis(evolving)
    assert database.connection.execute("SELECT COUNT(*) FROM pattern_observations WHERE pattern_instance_id='Double Top:stable'").fetchone()[0] == 2
    assert database.connection.execute("SELECT lifecycle FROM pattern_current WHERE pattern_instance_id='Double Top:stable'").fetchone()[0] == "FAILED"
    database.close()
    reopened = Database(tmp_path / "phase2b.sqlite")
    status = reopened.get_phase2b_status("TEST", "1H")
    assert status["structure"] is not None
    reopened.close()


def test_pattern_outcome_schema_does_not_invent_future_data(tmp_path):
    database = Database(tmp_path / "phase2b-outcomes.sqlite")
    database.connection.execute("INSERT INTO pattern_outcomes (instrument_id,timeframe,pattern_name,pattern_start,horizon,reference_price,created_at) VALUES (?,?,?,?,?,?,datetime('now'))", ("TEST", "1H", "Doji", "2025-02-01T00:00:00+00:00", "4H", "100"))
    row = database.connection.execute("SELECT future_price, max_favourable_move FROM pattern_outcomes").fetchone()
    assert row == (None, None)
    database.close()


def test_old_pattern_schema_migrates_transactionally_and_drops_legacy_table(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE pattern_observations (instrument_id TEXT, timeframe TEXT, candle_start TEXT, pattern_name TEXT, pattern_schema_version TEXT, lifecycle TEXT, is_closed INTEGER, observation_json TEXT, updated_at TEXT)")
    connection.execute("INSERT INTO pattern_observations VALUES (?,?,?,?,?,?,?,?,?)", ("TEST", "1H", "2025-02-01T00:00:00+00:00", "Double Top", "patterns_v1", "POTENTIAL", 1, "{}", "now"))
    connection.commit()
    connection.close()
    database = Database(path)
    columns = {row[1] for row in database.connection.execute("PRAGMA table_info(pattern_observations)")}
    legacy_count = database.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='pattern_observations_legacy'").fetchone()[0]
    assert "pattern_instance_id" in columns
    assert legacy_count == 0
    assert database.connection.execute("SELECT COUNT(*) FROM pattern_observations").fetchone()[0] == 1
    database.close()
