from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ig_ai.database import Database
from ig_ai.direction import DirectionScoreEngine
from ig_ai.forward import ForwardTestEngine
from ig_ai.models import Instrument
from ig_ai.runtime import PersistedStream

INSTRUMENT = Instrument("EPIC", "EPIC", "US Tech 100")


def update(timestamp: datetime, price: str = "100") -> dict:
    return {
        "instrument_id": "EPIC",
        "BIDPRICE1": price,
        "ASKPRICE1": price,
        "TIMESTAMP": timestamp.isoformat(),
    }


def _persist_forming(path, timestamp: datetime) -> None:
    database = Database(path)
    sink = PersistedStream(database, {"EPIC": INSTRUMENT}, session_started_at=timestamp)
    sink.on_update(update(timestamp))
    for aggregator in sink.aggregators.values():
        for candle in aggregator.flush():
            database.save_candle(candle)
    database.close()


def test_stale_forming_1h_and_15m_are_not_promoted_or_forwarded(tmp_path):
    path = tmp_path / "stale.sqlite3"
    old_runtime = datetime(2026, 9, 28, 9, 10, tzinfo=UTC)
    _persist_forming(path, old_runtime)

    database = Database(path)
    resumed = PersistedStream(
        database,
        {"EPIC": INSTRUMENT},
        session_started_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    resumed.on_update(update(datetime(2026, 9, 30, 0, 5, tzinfo=UTC)))

    assert resumed.stale_forming_candles
    assert database.connection.execute(
        "SELECT is_closed FROM candles WHERE instrument_id='EPIC' AND timeframe='1H' AND start_at=?",
        ("2026-09-28T09:00:00+00:00",),
    ).fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 0
    assert {row[0] for row in database.connection.execute(
        "SELECT timeframe FROM candles WHERE instrument_id='EPIC' AND start_at='2026-09-28T09:00:00+00:00'"
    )} == {"15M", "1H"}
    database.close()


def test_restart_same_open_bucket_is_closed_audit_only_not_analytical(tmp_path):
    path = tmp_path / "same-bucket.sqlite3"
    start = datetime(2026, 9, 30, 9, 5, tzinfo=UTC)
    _persist_forming(path, start)

    database = Database(path)
    resumed = PersistedStream(
        database,
        {"EPIC": INSTRUMENT},
        session_started_at=datetime(2026, 9, 30, 9, 10, tzinfo=UTC),
    )
    resumed.on_update(update(datetime(2026, 9, 30, 9, 20, tzinfo=UTC), "101"))
    resumed.on_update(update(datetime(2026, 9, 30, 10, 0, tzinfo=UTC), "102"))

    assert database.connection.execute(
        "SELECT is_closed FROM candles WHERE instrument_id='EPIC' AND timeframe='1H' AND start_at='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == 1
    assert database.connection.execute(
        "SELECT eligibility FROM candle_quality WHERE instrument_id='EPIC' AND timeframe='1H' AND start_at='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == "AUDIT_ONLY"
    assert [candle for candle in database.list_candles("EPIC", "1H") if candle.is_closed] == []
    assert database.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 0
    database.close()


def test_partial_first_hour_is_ineligible_next_full_hour_is_one_natural_snapshot(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    session_start = datetime(2026, 9, 30, 9, 30, tzinfo=UTC)
    database = Database(path)
    sink = PersistedStream(database, {"EPIC": INSTRUMENT}, session_started_at=session_start)
    sink.on_update(update(datetime(2026, 9, 30, 9, 35, tzinfo=UTC)))
    sink.on_update(update(datetime(2026, 9, 30, 10, 0, tzinfo=UTC)))
    assert database.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 0
    assert [candle for candle in database.list_candles("EPIC", "1H") if candle.is_closed] == []
    assert database.connection.execute(
        "SELECT COUNT(*) FROM technical_features WHERE instrument_id='EPIC' AND timeframe='1H' AND candle_start='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == 0
    assert database.connection.execute(
        "SELECT COUNT(*) FROM direction_snapshots WHERE instrument_id='EPIC' AND timeframe='1H' AND candle_start='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == 0
    assert database.connection.execute(
        "SELECT COUNT(*) FROM pattern_observations WHERE instrument_id='EPIC' AND timeframe='1H' AND candle_start='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == 0
    assert database.connection.execute(
        "SELECT COUNT(*) FROM structure_states WHERE instrument_id='EPIC' AND timeframe='1H' AND candle_start='2026-09-30T09:00:00+00:00'"
    ).fetchone()[0] == 0

    sink.on_update(update(datetime(2026, 9, 30, 11, 0, tzinfo=UTC)))
    row = database.connection.execute(
        "SELECT reference_time, created_at FROM forward_snapshots"
    ).fetchone()
    assert row[0] == "2026-09-30T11:00:00+00:00"
    assert datetime.fromisoformat(row[1]).astimezone(UTC) >= session_start
    assert database.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 1
    assert [candle.start.isoformat() for candle in database.list_candles("EPIC", "1H") if candle.is_closed] == [
        "2026-09-30T10:00:00+00:00"
    ]
    database.close()


def test_valid_ig_historical_closed_candle_remains_eligible(tmp_path):
    from ig_ai.models import Candle

    database = Database(tmp_path / "historical-quality.sqlite3")
    historical = Candle(
        "EPIC", "EPIC", "1H", datetime(2026, 9, 28, 9, tzinfo=UTC),
        datetime(2026, 9, 28, 10, tzinfo=UTC), Decimal("100"), Decimal("101"),
        Decimal("99"), Decimal("100"), is_closed=True, observation_count=4,
    )
    assert database.save_historical_candle(historical, provenance={"provider": "IG"}) == "INSERTED"
    listed = database.list_candles("EPIC", "1H")
    assert len(listed) == 1 and listed[0].start == historical.start and listed[0].is_closed
    assert database.connection.execute(
        "SELECT source, eligibility FROM candle_provenance p JOIN candle_quality q USING (instrument_id, timeframe, start_at)"
    ).fetchone() == ("IG_HISTORICAL", "ELIGIBLE")
    database.close()


def test_retroactive_snapshot_is_preserved_but_excluded_from_live_state_and_outcomes(tmp_path):
    path = tmp_path / "quarantine.sqlite3"
    database = Database(path)
    database.save_instrument_model(INSTRUMENT)
    reference = datetime(2026, 9, 28, 10, tzinfo=UTC)
    engine = ForwardTestEngine(database)
    from ig_ai.models import Candle

    candle = Candle("EPIC", "EPIC", "1H", reference - timedelta(hours=1), reference, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), is_closed=True)
    engine.record_snapshot(reference_candle=candle, coordinated={"direction_score": {}, "timeframes": {}})
    database.connection.execute(
        "UPDATE forward_snapshots SET created_at=?",
        ((reference + timedelta(days=2)).isoformat(),),
    )
    database.connection.execute("DELETE FROM forward_snapshot_status")
    database.connection.commit()
    database.close()

    database = Database(path)
    assert database.connection.execute("SELECT COUNT(*) FROM forward_snapshots").fetchone()[0] == 1
    assert database.connection.execute(
        "SELECT eligibility FROM forward_snapshot_status"
    ).fetchone()[0] == "INVALID_STALE"
    assert database.get_forward_state() is None
    assert database.pending_forward_snapshots() == []
    database.close()


def test_neutral_zero_evidence_is_explicitly_insufficient():
    neutral = {
        timeframe: {
            "instrument": "EPIC",
            "timeframe": timeframe,
            "candle_timestamp": "2026-09-30T10:00:00+00:00",
            "candle_state": "CLOSED",
            "structure": {"direction": "RANGE", "confirmed_highs": [], "confirmed_lows": []},
            "context": {},
            "divergences": [],
            "candlestick_patterns": [],
            "chart_patterns": [],
        }
        for timeframe in ("15M", "1H", "4H", "1D")
    }
    result = DirectionScoreEngine().analyze(neutral)
    assert (result["up_score"], result["down_score"]) == (0.0, 0.0)
    assert result["trend_stage"] == "INSUFFICIENT_EVIDENCE"
    assert result["reversal_risk"]["category"] == "UNKNOWN"
    assert result["holding_window"] == "UNKNOWN"
