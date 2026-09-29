from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ig_ai.database import Database
from ig_ai.history import (
    HistoricalIGClient,
    aggregate_candles,
    normalize_historical_price,
    report_gaps,
)
from ig_ai.models import Candle


def row(timestamp="2026-01-01T00:00:00Z"):
    return {
        "snapshotTimeUTC": timestamp,
        "openPrice": {"bid": 99, "ask": 101},
        "highPrice": {"bid": 104, "ask": 106},
        "lowPrice": {"bid": 94, "ask": 96},
        "closePrice": {"bid": 102, "ask": 104},
    }


def test_normalization_uses_midpoint_and_utc():
    candle = normalize_historical_price(row(), instrument_id="ig:EPIC", epic="EPIC", timeframe="15M")
    assert candle.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert candle.open == Decimal("100") and candle.close == Decimal("103")
    assert candle.volume is None and candle.is_closed


def test_invalid_ohlc_is_rejected():
    bad = row()
    bad["lowPrice"] = {"bid": 110, "ask": 111}
    with pytest.raises(ValueError):
        normalize_historical_price(bad, instrument_id="ig:EPIC", epic="EPIC", timeframe="1H")


def test_four_hour_aggregation_does_not_fabricate_partial_group():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [Candle("ig:E", "E", "1H", start + timedelta(hours=i), start + timedelta(hours=i + 1), Decimal(100 + i), Decimal(102 + i), Decimal(99 + i), Decimal(101 + i), is_closed=True) for i in range(4)]
    result = aggregate_candles(candles)
    assert len(result) == 1 and result[0].open == 100 and result[0].close == 104


def test_gap_report_is_explicit():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = Candle("ig:E", "E", "1H", start, start + timedelta(hours=1), Decimal(1), Decimal(2), Decimal(0), Decimal(1), is_closed=True)
    third = Candle("ig:E", "E", "1H", start + timedelta(hours=3), start + timedelta(hours=4), Decimal(1), Decimal(2), Decimal(0), Decimal(1), is_closed=True)
    report = report_gaps([first, third])
    assert report.missing_intervals == 1


def test_historical_conflict_is_idempotent_and_does_not_replace_live(tmp_path):
    database = Database(tmp_path / "history.sqlite3")
    candle = normalize_historical_price(row(), instrument_id="ig:E", epic="E", timeframe="15M")
    database.save_candle(candle)
    assert database.save_historical_candle(candle, provenance={"provider": "IG"}) == "SKIPPED_LIVE_CONFLICT"
    database.close()


def test_dry_run_adapter_does_not_require_persistence(tmp_path):
    class Client:
        def search_markets(self, term):
            return [{"epic": "EPIC", "name": term, "instrumentType": "INDICES", "expiry": "DFB"}]

        def market_details(self, epic):
            return {"instrument": {"name": "US Tech 100", "type": "INDICES", "expiry": "DFB"}, "snapshot": {"marketStatus": "TRADEABLE"}}

        def ensure_session(self):
            return object()

    class Wrapper:
        client = Client()

        def search_markets(self, term):
            return self.client.search_markets(term)

        def market_details(self, epic):
            return self.client.market_details(epic)

    from ig_ai.history import BackfillService
    database = Database(tmp_path / "dry.sqlite3")
    result = BackfillService(database, HistoricalIGClient(Wrapper(), pacing_seconds=0)).run(market="US Tech 100", timeframe="1H", start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 2, tzinfo=UTC), dry_run=True)
    assert result["status"] == "DRY_RUN"
    assert database.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0
    database.close()
