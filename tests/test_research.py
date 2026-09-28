from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ig_ai.database import Database
from ig_ai.models import Candle, Instrument
from ig_ai.research import (
    HORIZON_MINUTES,
    RESEARCH_ENGINE_VERSION,
    HistoricalReplay,
    OutcomeCalculator,
    ResearchConfig,
    ResearchEngine,
    regime_tags,
    sample_quality,
    score_bucket,
    window_start,
)
from ig_ai.technical import FEATURE_SCHEMA_VERSION

START = datetime(2026, 1, 1, tzinfo=UTC)


def candles(closes=(101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133)):
    result = []
    for index, close in enumerate(closes, start=1):
        start = START + timedelta(minutes=15 * (index - 1))
        result.append(Candle("TEST", "TEST.EPIC", "15M", start, start + timedelta(minutes=15), Decimal(close - 1), Decimal(close + 2), Decimal(close - 3), Decimal(close), is_closed=True))
    return result


def direction_state(direction="UP", timestamp="2026-01-01T00:00:00+00:00"):
    return {
        "instrument": "TEST", "direction": direction, "up_score": 72, "down_score": 28,
        "coverage": {"ratio": 0.8}, "trend_stage": "CONFIRMED", "reversal_risk": {"category": "LOW"},
        "holding_window": "4–8H", "timeframe_agreement": {"status": "AGREEMENT", "states": {"15M": "UP", "1H": "UP", "4H": "UP", "1D": "UP"}},
        "candle_timestamp": timestamp, "candle_state": "CLOSED", "model_reference": {"timeframe": "1H", "candle_timestamp": timestamp, "candle_state": "CLOSED"},
        "score_version": "direction_score_v1", "feature_schema_version": "technical_features_v1",
    }


def test_outcomes_cover_horizons_and_mfe_mae():
    calculator = OutcomeCalculator()
    result = calculator.calculate(reference_price=100, reference_time=START, direction="UP", candles=candles(), horizon="1H")
    assert result["status"] == "COMPLETE"
    assert result["future_price"] == 104
    assert result["direction_outcome"] == "UP"
    assert result["mfe"] == 6
    assert result["mae"] == -2
    assert result["time_to_mfe"] == 3600
    assert result["time_to_mae"] == 900
    assert result["time_to_reversal"] is None


def test_down_flat_and_reversal_outcomes_are_directional():
    down = OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="DOWN", candles=candles((99, 98, 97, 96)), horizon="1H")
    assert down["direction_outcome"] == "DOWN"
    assert down["mfe"] == 7
    assert down["mae"] == -1
    flat = OutcomeCalculator(ResearchConfig(flat_threshold=0.01)).calculate(reference_price=100, reference_time=START, direction="UP", candles=candles((100.05, 100.02, 100.04, 100.03)), horizon="1H")
    assert flat["direction_outcome"] == "FLAT"
    reversing = OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=candles((101, 102, 99, 98)), horizon="1H")
    assert reversing["time_to_reversal"] == 2700


def test_pending_future_outcome_stays_null_and_no_future_leakage():
    result = OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=candles((101,)), horizon="4H")
    assert result["status"] == "PENDING"
    assert result["future_price"] is None
    assert result["mfe"] is None


def test_all_requested_horizons_are_explicit():
    assert set(HORIZON_MINUTES) == {"15M", "1H", "2H", "4H", "8H", "1D"}
    with pytest.raises(ValueError):
        OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=candles(), horizon="3H")


def test_horizon_boundaries_require_exact_closed_end_and_never_use_after_horizon():
    long = candles(tuple(101 + index for index in range(100)))
    for horizon in ("15M", "1H", "2H", "4H", "8H", "1D"):
        result = OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=long, horizon=horizon)
        assert result["status"] == "COMPLETE"
        target_seconds = HORIZON_MINUTES[horizon] * 60
        assert result["future_timestamp"] == (START + timedelta(seconds=target_seconds)).isoformat()
    incomplete = list(long)
    incomplete[3] = Candle("TEST", "TEST.EPIC", "15M", incomplete[3].start, incomplete[3].end, incomplete[3].open, incomplete[3].high, incomplete[3].low, incomplete[3].close, is_closed=False)
    assert OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=incomplete, horizon="1H")["status"] == "PENDING"
    bounded = candles((101, 102, 103, 104, 200))
    result = OutcomeCalculator().calculate(reference_price=100, reference_time=START, direction="UP", candles=bounded, horizon="1H")
    assert result["mfe"] == 6


def test_direction_research_uses_closed_1h_end_as_information_time(tmp_path):
    database = Database(tmp_path / "alignment.sqlite")
    state = direction_state()
    state["schema_version"] = "direction_score_v1"
    state["timeframe"] = "1H"
    database.save_direction_snapshot(state)
    one_hour = Candle("TEST", "TEST.EPIC", "1H", START, START + timedelta(hours=1), Decimal(100), Decimal(101), Decimal(99), Decimal(100), is_closed=True)
    database.save_candle(one_hour)
    for candle in candles():
        database.save_candle(candle)
    result = ResearchEngine(database).run_direction_research(instrument_id="TEST", window="3Y", now=START + timedelta(days=365 * 3))
    assert result["recorded_snapshots"] == 1
    snapshot = database.connection.execute("SELECT snapshot_timestamp, reference_time FROM research_snapshots").fetchone()
    outcome = database.connection.execute("SELECT future_timestamp FROM research_outcomes WHERE horizon='15M'").fetchone()
    assert snapshot == (START.isoformat(), (START + timedelta(hours=1)).isoformat())
    assert outcome[0] == (START + timedelta(hours=1, minutes=15)).isoformat()
    database.close()


def test_score_buckets_sample_quality_and_windows():
    assert score_bucket(50) == "50–55"
    assert score_bucket(74.9) == "70–75"
    assert score_bucket(100) == "90–100"
    assert sample_quality(0) == "INSUFFICIENT_SAMPLE"
    assert sample_quality(30) == "EARLY_SAMPLE"
    assert sample_quality(100) == "USABLE_SAMPLE"
    assert window_start(START, "1Y") > window_start(START, "3Y") > window_start(START, "5Y")
    with pytest.raises(ValueError):
        window_start(START, "10Y")


def test_regime_tags_are_deterministic_and_not_probabilities():
    assert regime_tags({"direction": "UP", "trend_stage": "CONFIRMED"}, volatility_comparison="higher_than_recent")["trend_regime"] == "MODEL_STATE_UP"
    assert regime_tags({"technical_context": {"moving_averages": {"ema20": {"price_relation": "above"}, "ema50": {"price_relation": "above"}}}}, volatility_comparison="higher_than_recent")["trend_regime"] == "TRENDING_UP"
    assert regime_tags({"direction": "DOWN", "trend_stage": "REVERSAL_WATCH"}, volatility_comparison="lower_than_recent")["trend_regime"] == "MODEL_STATE_DOWN"


def test_research_snapshot_outcomes_are_versioned_idempotent_and_source_aware(tmp_path):
    database = Database(tmp_path / "research.sqlite")
    engine = ResearchEngine(database)
    snapshot_id = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    assert snapshot_id == engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    engine.complete_snapshot(snapshot_id, reference_time=START, reference_price=100, direction="UP", candles=candles((101, 102, 103, 104)), horizons=("15M", "1H", "2H"))
    engine.complete_snapshot(snapshot_id, reference_time=START, reference_price=100, direction="UP", candles=candles((101, 102, 103, 104)), horizons=("15M", "1H", "2H"))
    row = database.connection.execute("SELECT source_identity, research_version, reference_time FROM research_snapshots").fetchone()
    assert row == ("ig_cfd|instrument=TEST|epic=unknown", RESEARCH_ENGINE_VERSION, START.isoformat())
    assert database.connection.execute("SELECT COUNT(*) FROM research_outcomes").fetchone()[0] == 3
    database.close()


def test_source_epic_identity_and_pattern_alert_divergence_identity_are_distinct(tmp_path):
    database = Database(tmp_path / "identity.sqlite")
    database.save_instrument_model(Instrument("TEST", "IX.TEST.IP", "Test CFD", "CFD", "TRADEABLE"))
    engine = ResearchEngine(database)
    pattern_one = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="PATTERN", state=direction_state(), pattern_name="Double Top", pattern_lifecycle="POTENTIAL", pattern_instance_id="instance-a")
    pattern_two = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="PATTERN", state=direction_state(), pattern_name="Double Top", pattern_lifecycle="CONFIRMED", pattern_instance_id="instance-b")
    divergence = engine.record_snapshot(instrument_id="TEST", timeframe="15M", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIVERGENCE", state=direction_state(), divergence_indicator="RSI", divergence_direction="BEARISH")
    alert = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="ALERT", state=direction_state(), alert_type="EARLY_REVERSAL_WARNING", alert_id="alert-a", alert_event_identity="event-a")
    assert len({pattern_one, pattern_two, divergence, alert}) == 4
    source = database.connection.execute("SELECT source_identity FROM research_snapshots WHERE snapshot_id=?", (pattern_one,)).fetchone()[0]
    assert "epic=IX.TEST.IP" in source and "instrument=TEST" in source
    database.close()


def test_pending_outcomes_are_excluded_from_sample_summary(tmp_path):
    database = Database(tmp_path / "summary.sqlite")
    engine = ResearchEngine(database)
    snapshot = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    engine.complete_snapshot(snapshot, reference_time=START, reference_price=100, direction="UP", candles=candles((101,)), horizons=("4H",))
    assert engine.summary(instrument_id="TEST")["sample_count"] == 0
    assert engine.summary(instrument_id="TEST")["quality"] == "INSUFFICIENT_SAMPLE"
    database.close()


def test_research_outcome_batch_is_atomic_and_rerunnable(tmp_path, monkeypatch):
    database = Database(tmp_path / "atomic.sqlite")
    engine = ResearchEngine(database)
    snapshot = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    outcomes = {horizon: engine.calculator.calculate(reference_price=100, reference_time=START, direction="UP", candles=candles(), horizon=horizon) for horizon in ("15M", "1H")}
    original = database._save_research_outcome
    calls = {"count": 0}
    def interrupted(connection, snapshot_id, horizon, outcome):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated interruption")
        return original(connection, snapshot_id, horizon, outcome)
    monkeypatch.setattr(database, "_save_research_outcome", interrupted)
    with pytest.raises(RuntimeError):
        database.save_research_outcomes(snapshot, outcomes)
    assert database.connection.execute("SELECT COUNT(*) FROM research_outcomes").fetchone()[0] == 0
    monkeypatch.setattr(database, "_save_research_outcome", original)
    database.save_research_outcomes(snapshot, outcomes)
    database.save_research_outcomes(snapshot, outcomes)
    assert database.connection.execute("SELECT COUNT(*) FROM research_outcomes").fetchone()[0] == 2
    database.close()


def test_research_dimensions_remain_separate(tmp_path):
    database = Database(tmp_path / "dimensions.sqlite")
    engine = ResearchEngine(database)
    base = direction_state()
    engine.record_snapshot(instrument_id="TEST", timeframe="15M", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="PATTERN", state=base, pattern_name="Double Top", pattern_lifecycle="POTENTIAL")
    engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=(START + timedelta(hours=1)).isoformat(), reference_price=100, snapshot_type="DIVERGENCE", state={**base, "direction": "DOWN"}, alert_type="EARLY_REVERSAL_WARNING")
    rows = database.get_research_snapshots(instrument_id="TEST", timeframe="15M", pattern_name="Double Top")
    assert len(rows) == 1 and rows[0]["pattern_lifecycle"] == "POTENTIAL"
    assert rows[0]["four_hour_alignment"] == "UP"
    assert database.get_research_snapshots(instrument_id="TEST", timeframe="1H", alert_type="EARLY_REVERSAL_WARNING")[0]["direction"] == "DOWN"
    database.close()


def test_available_history_reports_insufficient_history(tmp_path):
    database = Database(tmp_path / "history.sqlite")
    assert database.available_history("TEST", "1H")["status"] == "INSUFFICIENT_HISTORY"
    candle = Candle("TEST", "TEST.EPIC", "1H", START, START + timedelta(hours=1), Decimal(100), Decimal(101), Decimal(99), Decimal(100), is_closed=True)
    database.save_candle(candle)
    assert database.available_history("TEST", "1H", requested_window="3Y")["status"] == "INSUFFICIENT_HISTORY"
    database.close()


def test_research_summary_is_as_of_reproducible_and_reports_horizon_n(tmp_path):
    database = Database(tmp_path / "metrics.sqlite")
    engine = ResearchEngine(database)
    snapshot = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    engine.complete_snapshot(snapshot, reference_time=START, reference_price=100, direction="UP", candles=candles(), horizons=("1H",))
    summary = engine.summary(window="1Y", as_of=START + timedelta(days=365), instrument_id="TEST", timeframe="1H")
    row = summary["rows"][0]
    assert summary["as_of"] == (START + timedelta(days=365)).isoformat()
    assert summary["sample_count"] == 1 and summary["outcome_observation_count"] == 1
    assert row["sample_count"] == 1 and row["up_percentage"] == 100.0
    assert row["median_mfe"] is not None and row["median_mae"] is not None
    database.close()


def test_four_hour_comparison_is_explicit_readiness_only(tmp_path):
    database = Database(tmp_path / "four-hour.sqlite")
    engine = ResearchEngine(database)
    with_4h = direction_state()
    without_4h = {**direction_state(), "timeframe_agreement": {"status": "PARTIAL_AGREEMENT", "states": {"1H": "UP", "15M": "UP", "1D": "UP"}}}
    first = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_price=100, snapshot_type="DIRECTION", state=with_4h)
    second = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=(START + timedelta(hours=1)).isoformat(), reference_price=100, snapshot_type="DIRECTION", state=without_4h)
    for snapshot in (first, second):
        engine.complete_snapshot(snapshot, reference_time=START, reference_price=100, direction="UP", candles=candles(), horizons=("1H",))
    comparison = database.research_4h_comparison(instrument_id="TEST", horizon="1H")
    assert comparison["status"].startswith("READINESS_ONLY")
    assert comparison["WITH_4H"]["snapshot_count"] == 1 and comparison["WITHOUT_4H"]["snapshot_count"] == 1


def test_historical_replay_is_point_in_time_and_deterministic():
    def timeframe_candles(timeframe, minutes, count):
        result = []
        for index in range(count):
            start = START + timedelta(minutes=minutes * index)
            close = Decimal(100 + index)
            result.append(Candle("TEST", "TEST.EPIC", timeframe, start, start + timedelta(minutes=minutes), close - 1, close + 2, close - 3, close, is_closed=True))
        return result

    replay_input = {
        "15M": timeframe_candles("15M", 15, 100),
        "1H": timeframe_candles("1H", 60, 20),
        "4H": timeframe_candles("4H", 240, 10),
        "1D": timeframe_candles("1D", 1440, 5),
    }
    first = HistoricalReplay().direction_states(replay_input)
    second = HistoricalReplay().direction_states(replay_input)
    assert len(first) == len(second) == 20
    assert [item["state"] for item in first] == [item["state"] for item in second]
    for item in first:
        candle = item["reference_candle"]
        model_reference = item["state"]["model_reference"]
        assert model_reference["candle_timestamp"] == candle.start.isoformat()
        assert model_reference["information_time"] == candle.end.isoformat()
        assert all(candidate.end <= candle.end for candidates in item["histories"].values() for candidate in candidates)


def test_research_persistence_survives_database_restart(tmp_path):
    path = tmp_path / "restart.sqlite"
    database = Database(path)
    engine = ResearchEngine(database)
    snapshot_id = engine.record_snapshot(instrument_id="TEST", timeframe="1H", snapshot_timestamp=START.isoformat(), reference_time=(START + timedelta(hours=1)).isoformat(), reference_price=100, snapshot_type="DIRECTION", state=direction_state())
    engine.complete_snapshot(snapshot_id, reference_time=START + timedelta(hours=1), reference_price=100, direction="UP", candles=candles(), horizons=("1H",))
    database.close()
    reopened = Database(path)
    assert reopened.completed_research_snapshot_count(instrument_id="TEST", timeframe="1H") == 1
    assert reopened.research_summary(instrument_id="TEST", timeframe="1H", horizon="1H")[0]["sample_count"] == 1
    reopened.close()
    database.close()


def test_explicit_direction_research_job_records_and_completes_outcomes(tmp_path):
    database = Database(tmp_path / "job.sqlite")
    state = direction_state()
    state["schema_version"] = "direction_score_v1"
    state["timeframe"] = "1H"
    database.save_direction_snapshot(state)
    database.save_technical_features({"instrument": "TEST", "timeframe": "1H", "candle_timestamp": START.isoformat(), "schema_version": FEATURE_SCHEMA_VERSION, "candle_state": "CLOSED", "candle": {"close": 100}, "atr14": {"recent_comparison": "normal"}})
    one_hour = Candle("TEST", "TEST.EPIC", "1H", START, START + timedelta(hours=1), Decimal(100), Decimal(101), Decimal(99), Decimal(100), is_closed=True)
    database.save_candle(one_hour)
    for candle in candles():
        database.save_candle(candle)
    result = ResearchEngine(database).run_direction_research(instrument_id="TEST", window="3Y", now=START + timedelta(days=1))
    assert result["recorded_snapshots"] == 1
    assert result["outcomes_written"] == 6
    assert database.connection.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0] == 1
    database.close()
