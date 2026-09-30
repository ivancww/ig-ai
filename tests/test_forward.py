from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ig_ai.database import Database
from ig_ai.forward import FORWARD_MODEL_VERSION, ForwardTestEngine
from ig_ai.models import Candle, Instrument

START = datetime(2026, 1, 1, tzinfo=UTC)


def candle(start: datetime, close: str, *, high: str | None = None, low: str | None = None) -> Candle:
    price = Decimal(close)
    return Candle("EPIC", "IX.TEST", "15M", start, start + timedelta(minutes=15), price, Decimal(high or close), Decimal(low or close), price, is_closed=True)


def coordinated() -> dict:
    return {
        "direction_score": {
            "instrument": "EPIC", "direction": "UP", "up_score": 70, "down_score": 30,
            "trend_stage": "CONFIRMED", "holding_window": "2–4H",
            "reversal_risk": {"category": "LOW"},
        },
        "timeframes": {
            "1H": {"context": {"structure": "UPTREND"}, "candlestick_patterns": [], "chart_patterns": []},
            "15M": {"candlestick_patterns": [], "chart_patterns": [], "divergences": [{"pattern": "LH"}]},
        },
    }


def test_live_forward_snapshot_is_immutable_and_point_in_time(tmp_path):
    db = Database(tmp_path / "forward.sqlite3")
    db.save_instrument_model(Instrument("EPIC", "IX.TEST", "US Tech 100", "INDICES"))
    engine = ForwardTestEngine(db)
    reference = Candle("EPIC", "IX.TEST", "1H", START, START + timedelta(hours=1), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), is_closed=True)
    snapshot_id = engine.record_snapshot(reference_candle=reference, coordinated=coordinated())
    assert engine.record_snapshot(reference_candle=reference, coordinated={**coordinated(), "direction_score": {"direction": "DOWN"}}) == snapshot_id
    row = db.connection.execute("SELECT provenance, market, reference_time, model_version FROM forward_snapshots").fetchone()
    assert row == ("LIVE_FORWARD", "US Tech 100", (START + timedelta(hours=1)).isoformat(), FORWARD_MODEL_VERSION)
    assert db.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0] == 0


def test_forward_outcomes_stay_pending_until_exact_closed_horizon_then_complete(tmp_path):
    db = Database(tmp_path / "outcomes.sqlite3")
    db.save_instrument_model(Instrument("EPIC", "IX.TEST", "US Tech 100", "INDICES"))
    engine = ForwardTestEngine(db)
    reference_time = START + timedelta(hours=1)
    reference = Candle("EPIC", "IX.TEST", "1H", START, reference_time, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), is_closed=True)
    snapshot_id = engine.record_snapshot(reference_candle=reference, coordinated=coordinated())
    future = [candle(reference_time, "101", high="102", low="99.5")]
    engine.update_outcomes(instrument_id="EPIC", candles=future)
    assert db.connection.execute("SELECT status FROM forward_outcomes WHERE snapshot_id=? AND horizon='15M'", (snapshot_id,)).fetchone()[0] == "COMPLETE"
    assert db.connection.execute("SELECT status FROM forward_outcomes WHERE snapshot_id=? AND horizon='1H'", (snapshot_id,)).fetchone()[0] == "PENDING"
    assert db.connection.execute("SELECT mfe, mae FROM forward_outcomes WHERE snapshot_id=? AND horizon='15M'", (snapshot_id,)).fetchone() == (2.0, -0.5)
    # A later rerun cannot rewrite a completed outcome.
    engine.update_outcomes(instrument_id="EPIC", candles=[*future, candle(reference_time + timedelta(minutes=15), "90")])
    assert db.connection.execute("SELECT future_price FROM forward_outcomes WHERE snapshot_id=? AND horizon='15M'", (snapshot_id,)).fetchone()[0] == 101.0


def test_forward_outcome_completion_counter_counts_transitions_once_and_resumes_pending_horizons(tmp_path):
    db = Database(tmp_path / "counter.sqlite3")
    db.save_instrument_model(Instrument("EPIC", "IX.TEST", "US Tech 100", "INDICES"))
    engine = ForwardTestEngine(db)
    reference_time = START + timedelta(hours=1)
    reference = Candle("EPIC", "IX.TEST", "1H", START, reference_time, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), is_closed=True)
    snapshot_id = engine.record_snapshot(reference_candle=reference, coordinated=coordinated())
    first = candle(reference_time, "101", high="102", low="99.5")

    assert engine.update_outcomes(instrument_id="EPIC", candles=[first]) == 1
    assert engine.update_outcomes(instrument_id="EPIC", candles=[first]) == 0
    assert db.connection.execute("SELECT status FROM forward_outcomes WHERE snapshot_id=? AND horizon='15M'", (snapshot_id,)).fetchone()[0] == "COMPLETE"
    assert db.connection.execute("SELECT status FROM forward_outcomes WHERE snapshot_id=? AND horizon='1H'", (snapshot_id,)).fetchone()[0] == "PENDING"

    hour_candles = [first] + [candle(reference_time + timedelta(minutes=15 * index), str(101 + index)) for index in range(1, 4)]
    assert engine.update_outcomes(instrument_id="EPIC", candles=hour_candles) == 1
    assert engine.update_outcomes(instrument_id="EPIC", candles=hour_candles) == 0
    assert db.connection.execute("SELECT status FROM forward_outcomes WHERE snapshot_id=? AND horizon='1H'", (snapshot_id,)).fetchone()[0] == "COMPLETE"


def test_forward_snapshot_resume_preserves_identity_and_does_not_cross_historical_boundary(tmp_path):
    db = Database(tmp_path / "resume.sqlite3")
    instrument = Instrument("EPIC", "IX.TEST", "US Tech 100", "INDICES")
    db.save_instrument_model(instrument)
    reference = Candle("EPIC", "IX.TEST", "1H", START, START + timedelta(hours=1), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), is_closed=True)
    first = ForwardTestEngine(db)
    snapshot_id = first.record_snapshot(reference_candle=reference, coordinated=coordinated())
    before = db.connection.execute("SELECT source_identity, provenance, model_version, direction_state_json FROM forward_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()

    resumed = ForwardTestEngine(db)
    assert resumed.record_snapshot(reference_candle=reference, coordinated={**coordinated(), "direction_score": {"direction": "DOWN"}}) == snapshot_id
    after = db.connection.execute("SELECT source_identity, provenance, model_version, direction_state_json FROM forward_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    assert after == before
    assert db.connection.execute("SELECT COUNT(*) FROM forward_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0] == 1

    historical_id = "historical-same-time"
    db.connection.execute(
        """INSERT INTO research_snapshots
        (snapshot_id, instrument_id, timeframe, snapshot_timestamp, reference_time, snapshot_type,
         source_identity, reference_price, trend_regime, volatility_regime, context_json,
         research_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (historical_id, "EPIC", "1H", START.isoformat(), (START + timedelta(hours=1)).isoformat(), "DIRECTION_REPLAY",
         "ig_cfd|instrument=EPIC|epic=IX.TEST", 100, "HISTORICAL", "HISTORICAL", "{}", "research_v1", START.isoformat()),
    )
    db.connection.commit()
    assert db.connection.execute("SELECT COUNT(*) FROM forward_snapshots WHERE reference_time=?", ((START + timedelta(hours=1)).isoformat(),)).fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM research_snapshots WHERE snapshot_id=?", (historical_id,)).fetchone()[0] == 1
