from datetime import UTC, datetime
from decimal import Decimal

from ig_ai.candles import CandleAggregator
from ig_ai.models import MarketObservation


def observation(ts: str, price: str) -> MarketObservation:
    return MarketObservation(
        datetime.fromisoformat(ts).replace(tzinfo=UTC),
        "id",
        "EPIC",
        "Market",
        Decimal(price),
        Decimal(price),
    )


def test_15m_forming_and_closed():
    agg = CandleAggregator("15M")
    assert agg.update(observation("2026-01-01T00:01:00", "100")) == []
    agg.update(observation("2026-01-01T00:14:00", "105"))
    forming = agg.forming(observation("2026-01-01T00:14:00", "105"))
    assert forming and not forming.is_closed and forming.high == Decimal("105")
    closed = agg.update(observation("2026-01-01T00:15:00", "103"))
    assert len(closed) == 1 and closed[0].is_closed
    assert closed[0].open == Decimal("100") and closed[0].close == Decimal("105")
    assert closed[0].observation_count == 2


def test_1h_4h_and_daily_boundaries():
    for timeframe, next_ts in (
        ("1H", "2026-01-01T01:00:00"),
        ("4H", "2026-01-01T04:00:00"),
        ("1D", "2026-01-02T00:00:00"),
    ):
        agg = CandleAggregator(timeframe)
        agg.update(observation("2026-01-01T00:00:00", "1"))
        closed = agg.update(observation(next_ts, "2"))
        assert closed and closed[0].timeframe == timeframe


def test_daily_market_timezone_and_dst_safe_timestamp():
    agg = CandleAggregator("1D", market_timezone="America/New_York")
    agg.update(observation("2026-03-08T05:59:00", "1"))
    closed = agg.update(observation("2026-03-09T04:00:00", "2"))
    assert closed[0].start.isoformat() == "2026-03-08T05:00:00+00:00"


def test_late_tick_does_not_mutate_current_candle():
    agg = CandleAggregator("15M")
    agg.update(observation("2026-01-01T00:10:00", "100"))
    agg.update(observation("2026-01-01T00:12:00", "102"))
    agg.update(observation("2026-01-01T00:11:00", "1"))
    assert agg.forming(observation("2026-01-01T00:12:00", "102")).low == Decimal("100")


def test_shutdown_flush_preserves_epic_and_does_not_close_incomplete_candle():
    agg = CandleAggregator("15M")
    agg.update(observation("2026-01-01T00:01:00", "100"))
    flushed = agg.flush()
    assert len(flushed) == 1
    assert flushed[0].epic == "EPIC"
    assert not flushed[0].is_closed
