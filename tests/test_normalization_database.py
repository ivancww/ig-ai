from datetime import UTC, datetime
from decimal import Decimal

from ig_ai.database import Database
from ig_ai.models import Instrument, MarketObservation
from ig_ai.normalization import normalize_price_update
from ig_ai.runtime import PersistedStream


def test_normalizes_mid_and_utc():
    result = normalize_price_update(
        {
            "UPDATE_TIME": "2026-01-01T10:00:00+08:00",
            "BID": "100",
            "OFFER": "102",
            "MARKET_STATE": "TRADEABLE",
        },
        instrument_id="1",
        epic="EPIC",
        market_name="Market",
    )
    assert result.timestamp == datetime(2026, 1, 1, 2, tzinfo=UTC)
    assert result.mid == Decimal("101")


def test_database_persists_observation(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    observation = MarketObservation(
        datetime.now(UTC), "1", "EPIC", "Market", Decimal("1"), Decimal("2")
    )
    db.save_observation(observation)
    assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    db.save_observation(observation)
    assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    db.close()


def test_epoch_millisecond_price_update_normalizes_bid_ask_and_mid():
    result = normalize_price_update(
        {
            "TIMESTAMP": "1760000000000",
            "BIDPRICE1": "100",
            "ASKPRICE1": "102",
            "DLG_FLAG": "DEAL",
        },
        instrument_id="1",
        epic="EPIC",
        market_name="Market",
    )
    assert result.bid == Decimal("100")
    assert result.offer == Decimal("102")
    assert result.mid == Decimal("101")
    assert result.market_state == "DEAL"


def test_persisted_stream_writes_observation_and_both_forming_candles(tmp_path):
    db = Database(tmp_path / "pipeline.sqlite3")
    instrument = Instrument("EPIC", "EPIC", "Market")
    sink = PersistedStream(db, {instrument.instrument_id: instrument})
    sink.on_update(
        {
            "instrument_id": "EPIC",
            "item": "1",
            "subscription_id": "1",
            "BIDPRICE1": "100",
            "ASKPRICE1": "102",
            "TIMESTAMP": "1760000000000",
            "DLG_FLAG": "DEAL",
        }
    )
    assert sink.observations_written == 1
    assert sink.candles_written == {"15M": 1, "1H": 1}
    assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM candles WHERE timeframe = '15M'").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM candles WHERE timeframe = '1H'").fetchone()[0] == 1
    assert sink.on_update(
        {
            "instrument_id": "EPIC",
            "BIDPRICE1": "bad-price",
            "ASKPRICE1": "102",
            "TIMESTAMP": "1760000000000",
        }
    ) == {"observation_created": "false", "skip_reason": "invalid_timestamp_or_price"}
    db.close()


def test_persisted_stream_records_safe_skip_reason_without_update_values(tmp_path):
    db = Database(tmp_path / "skip.sqlite3")
    instrument = Instrument("EPIC", "EPIC", "Market")
    sink = PersistedStream(db, {instrument.instrument_id: instrument})
    sink.on_update(
        {
            "instrument_id": "EPIC",
            "BIDPRICE1": "100",
            "TIMESTAMP": "1760000000000",
            "secret": "CST-private-token",
        }
    )
    assert sink.safe_skip_diagnostics == [{"instrument_id": "EPIC", "reason": "missing_bid_or_ask"}]
    assert "CST-private-token" not in str(sink.safe_skip_diagnostics)
    db.close()


def test_persisted_stream_classifies_null_price_snapshot_as_missing_timestamp(tmp_path):
    db = Database(tmp_path / "null-snapshot.sqlite3")
    instrument = Instrument("EPIC", "EPIC", "Market")
    sink = PersistedStream(db, {instrument.instrument_id: instrument})

    result = sink.on_update(
        {
            "instrument_id": "EPIC",
            "item": "1",
            "subscription_id": "1",
            "BIDPRICE1": None,
        }
    )

    assert result == {"observation_created": "false", "skip_reason": "missing_timestamp"}
    assert sink.safe_skip_diagnostics == [{"instrument_id": "EPIC", "reason": "missing_timestamp"}]
    db.close()
