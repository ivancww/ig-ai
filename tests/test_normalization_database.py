from datetime import UTC, datetime
from decimal import Decimal

from ig_ai.database import Database
from ig_ai.models import MarketObservation
from ig_ai.normalization import normalize_price_update


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
    db.close()
