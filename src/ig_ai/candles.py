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
    last_timestamp: datetime
    epic: str
    observation_count: int


class CandleAggregator:
    def __init__(self, timeframe: str, *, market_timezone: str = "UTC"):
        self.timeframe = timeframe.upper()
        self.market_timezone = ZoneInfo(market_timezone)
        if self.timeframe not in (*TIMEFRAME_MINUTES, "1D"):
            raise ValueError("timeframe must be 15M, 1H, 4H, or 1D")
        self._working: dict[str, _Working] = {}

    def restore(self, candle: Candle, *, session_started_at: datetime | None = None) -> bool:
        """Restore one incomplete candle when it is still open for this session."""
        if candle.is_closed or candle.timeframe != self.timeframe:
            return False
        if session_started_at is not None and candle.end <= as_utc(session_started_at):
            return False
        self._working[candle.instrument_id] = _Working(
            candle.start,
            candle.end,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.start,
            candle.epic,
            candle.observation_count,
        )
        return True

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
        timestamp = as_utc(observation.timestamp)
        if current and (start < current.start or timestamp < current.last_timestamp):
            # Ignore late/out-of-order ticks so historical data cannot mutate a
            # candle that has already advanced in time.
            return closed
        if current and start > current.start:
            closed.append(self._to_candle(observation, current, True))
            current = None
        if current is None:
            current = _Working(start, end, price, price, price, price, timestamp, observation.epic, 1)
            self._working[key] = current
        else:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            current.last_timestamp = timestamp
            current.observation_count += 1
        return closed

    def forming(self, observation: MarketObservation) -> Candle | None:
        working = self._working.get(observation.instrument_id)
        return self._to_candle(observation, working, False) if working else None

    def flush(self, *, instrument_id: str | None = None) -> list[Candle]:
        """Close currently forming candles during a controlled shutdown."""
        keys = [instrument_id] if instrument_id else list(self._working)
        result = []
        for key in keys:
            working = self._working.pop(key, None)
            if working:
                result.append(
                    Candle(
                        key, working.epic, self.timeframe, working.start, working.end,
                        working.open, working.high, working.low, working.close,
                        is_closed=False, observation_count=working.observation_count,
                    )
                )
        return result

    def _to_candle(
        self, observation: MarketObservation, working: _Working, is_closed: bool
    ) -> Candle:
        return Candle(
            observation.instrument_id,
            working.epic,
            self.timeframe,
            working.start,
            working.end,
            working.open,
            working.high,
            working.low,
            working.close,
            is_closed=is_closed,
            observation_count=working.observation_count,
        )
