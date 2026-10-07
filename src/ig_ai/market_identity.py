"""Canonical identities shared by discovery, runtime, and read-only views."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

CANONICAL_MARKETS = ("US Tech 100", "Japan 225", "Hong Kong HS50")

_ALIASES = {
    "US Tech 100": ("US Tech 100", "美國科技股100", "美國科技股100指數", "美国科技股100", "Nasdaq 100", "NASDAQ"),
    "Japan 225": ("Japan 225", "日本225", "日經225", "日経225", "NIKKEI"),
    "Hong Kong HS50": ("Hong Kong HS50", "Hong Kong 50", "香港HS50", "香港50", "HS50", "Hang Seng"),
}


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def canonical_market_label(value: str | None) -> str | None:
    """Return the configured market label for a provider/display name."""
    if not value:
        return None
    candidate = _normalized(value)
    for label in CANONICAL_MARKETS:
        for alias in _ALIASES[label]:
            normalized_alias = _normalized(alias)
            if candidate == normalized_alias or candidate.startswith(normalized_alias + " "):
                return label
    return None


def canonical_instrument_rows(rows: Iterable[tuple]) -> dict[str, tuple[str, str, str | None]]:
    """Index persisted instruments by canonical label, never by display locale."""
    resolved: dict[str, tuple[str, str, str | None]] = {}
    for row in rows:
        instrument_id, market_name, market_status, epic, metadata_json = row
        metadata: dict[str, Any]
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        label = next(
            (
                canonical_market_label(value)
                for value in (metadata.get("requested_market"), market_name, epic)
                if canonical_market_label(value)
            ),
            None,
        )
        if label is not None:
            resolved[label] = (instrument_id, epic, market_status)
    return resolved
