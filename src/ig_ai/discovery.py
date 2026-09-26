from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .rest import IGRestClient


@dataclass(frozen=True)
class DiscoveryCandidate:
    market_name: str
    epic: str
    market_status: str | None
    instrument_type: str | None
    metadata: dict[str, Any]


DEFAULT_SEARCH_TERMS = {
    "US Tech 100": "US Tech 100",
    "Japan 225": "Japan 225",
    "Hong Kong HS50": "Hong Kong HS50",
}


def discover_markets(
    client: IGRestClient, search_terms: dict[str, str] | None = None
) -> list[DiscoveryCandidate]:
    results: list[DiscoveryCandidate] = []
    for market_name, term in (search_terms or DEFAULT_SEARCH_TERMS).items():
        for item in client.search_markets(term):
            epic = str(item.get("epic", "")).strip()
            if epic:
                results.append(
                    DiscoveryCandidate(
                        market_name,
                        epic,
                        item.get("marketStatus"),
                        item.get("instrumentType"),
                        item,
                    )
                )
    return results
