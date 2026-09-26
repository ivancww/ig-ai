from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import MarketObservation


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid price value: {value!r}") from exc


def parse_timestamp(value: Any) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def normalize_price_update(
    update: dict[str, Any],
    *,
    instrument_id: str,
    epic: str,
    market_name: str,
    source: str = "ig-stream",
) -> MarketObservation:
    timestamp = parse_timestamp(update.get("UPDATE_TIME") or update.get("timestamp"))
    return MarketObservation(
        timestamp,
        instrument_id,
        epic,
        market_name,
        decimal_or_none(update.get("BID")),
        decimal_or_none(update.get("OFFER")),
        update.get("MARKET_STATE"),
        source,
    )
