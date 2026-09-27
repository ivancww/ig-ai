from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MarketObservation:
    timestamp: datetime
    instrument_id: str
    epic: str
    market_name: str
    bid: Decimal | None
    offer: Decimal | None
    market_state: str | None = None
    source: str = "ig"

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.offer is None:
            return None
        return (self.bid + self.offer) / Decimal(2)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))


@dataclass(frozen=True)
class Candle:
    instrument_id: str
    epic: str
    timeframe: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    is_closed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", as_utc(self.start))
        object.__setattr__(self, "end", as_utc(self.end))


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    epic: str
    market_name: str
    instrument_type: str | None = None
    market_status: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class VerifiedInstrument:
    """Provider-confirmed identity; never represents an official cash index."""

    requested_market: str
    market_name: str
    epic: str
    instrument_type: str | None
    market_status: str | None
    expiry: str | None
    classification: str
    metadata: dict
