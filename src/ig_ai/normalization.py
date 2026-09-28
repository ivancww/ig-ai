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
    if value in (None, ""):
        raise ValueError("timestamp is required")
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            try:
                result = datetime.fromtimestamp(float(text) / 1000, UTC)
            except (OverflowError, ValueError):
                # Lightstreamer commonly sends UPDATE_TIME as HH:MM:SS.
                result = datetime.combine(datetime.now(UTC).date(), datetime.strptime(text, "%H:%M:%S").time(), UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def normalize_price_update(
    update: dict[str, Any],
    *,
    instrument_id: str,
    epic: str,
    market_name: str,
    source: str = "IG_STREAMING",
) -> MarketObservation:
    timestamp = parse_timestamp(
        update.get("UPDATE_TIME")
        or update.get("UPDATE_TIMESTAMP")
        or update.get("PROVIDER_TIMESTAMP")
        or update.get("TIMESTAMP")
        or update.get("timestamp")
    )
    return MarketObservation(
        timestamp,
        instrument_id,
        epic,
        market_name,
        decimal_or_none(update.get("BID") or update.get("BIDPRICE1")),
        decimal_or_none(update.get("OFFER") or update.get("ASKPRICE1")),
        update.get("MARKET_STATE") or update.get("MARKET_STATUS") or update.get("DLG_FLAG"),
        source.upper() if source else "IG_STREAMING",
    )
