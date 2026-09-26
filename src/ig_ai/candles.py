from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import Candle, MarketObservation, as_utc

TIMEFRAME_MINUTES = {"15M": 15, "1H": 60, "4H": 240}


@dataclass
class _Working:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class CandleAggregator:
    def __init__(self, timeframe: str, *, market_timezone: str = "UTC"):
        self.timeframe = timeframe.upper()
        self.market_timezone = ZoneInfo(market_timezone)
        if self.timeframe not in (*TIMEFRAME_MINUTES, "1D"):
            raise ValueError("timeframe must be 15M, 1H, 4H, or 1D")
        self._working: dict[str, _Working] = {}

    def _bounds(self, timestamp: datetime) -> tuple[datetime, datetime]:
        local = as_utc(timestamp).astimezone(self.market_timezone)
        if self.timeframe == "1D":
            start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
        else:
            minutes = TIMEFRAME_MINUTES[self.timeframe]
            total = local.hour * 60 + local.minute
            start_local = local.replace(
                hour=total // 60, minute=total % 60, second=0, microsecond=0
            )
            start_local -= timedelta(minutes=total % minutes)
            end_local = start_local + timedelta(minutes=minutes)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def update(self, observation: MarketObservation) -> list[Candle]:
        price = observation.mid
        if price is None:
            return []
        key = observation.instrument_id
        start, end = self._bounds(observation.timestamp)
        current = self._working.get(key)
        closed: list[Candle] = []
        if current and start > current.start:
            closed.append(self._to_candle(observation, current, True))
            current = None
        if current is None:
            current = _Working(start, end, price, price, price, price)
            self._working[key] = current
        else:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
        return closed

    def forming(self, observation: MarketObservation) -> Candle | None:
        working = self._working.get(observation.instrument_id)
        return self._to_candle(observation, working, False) if working else None

    def _to_candle(
        self, observation: MarketObservation, working: _Working, is_closed: bool
    ) -> Candle:
        return Candle(
            observation.instrument_id,
            observation.epic,
            self.timeframe,
            working.start,
            working.end,
            working.open,
            working.high,
            working.low,
            working.close,
            is_closed=is_closed,
        )
