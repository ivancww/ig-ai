from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import VerifiedInstrument
from .rest import IGRestClient

SEARCH_TERMS = {
    "US Tech 100": ("US Tech 100", "Nasdaq 100", "US Tech"),
    "Japan 225": ("Japan 225", "Japan 225 Index", "Nikkei 225"),
    "Hong Kong HS50": ("Hong Kong HS50", "Hong Kong 50", "Hong Kong", "HS50", "Hang Seng"),
}


@dataclass(frozen=True)
class DiscoveryCandidate:
    requested_market: str
    market_name: str
    epic: str
    market_status: str | None
    instrument_type: str | None
    expiry: str | None
    classification: str
    eligible_primary: bool
    verified: bool
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MarketDiscovery:
    requested_market: str
    status: str
    candidates: tuple[DiscoveryCandidate, ...]


def _value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


def classify_instrument(data: dict[str, Any]) -> str:
    """Classify only from provider fields; UNKNOWN is safer than an assumption."""
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    expiry = str(_value(data, "expiry", "expiration") or "").upper()
    name = str(_value(data, "name", "marketName") or "").upper()
    if instrument_type in {"SHARES", "OPT_INDICES", "OPTIONS"}:
        return "OTHER"
    if instrument_type in {"INDICES", "INDEX"}:
        if expiry in {"DFB", "-", "NULL", "NONE"} or "ROLLING" in name or "CASH" in name:
            return "CASH/ROLLING CFD"
        if expiry and expiry not in {"DFB", "-", "NULL", "NONE"}:
            return "FUTURES/FORWARD"
        return "UNKNOWN"
    if expiry and expiry not in {"DFB", "-", "NULL", "NONE"}:
        return "FUTURES/FORWARD"
    return "OTHER" if instrument_type else "UNKNOWN"


def _eligible(data: dict[str, Any], classification: str) -> bool:
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    name = str(_value(data, "name", "marketName") or "").upper()
    return (
        instrument_type in {"INDICES", "INDEX"}
        and classification == "CASH/ROLLING CFD"
        and not any(term in name for term in ("OPTION", "ETF", "SHARE"))
    )


def _candidate(requested_market: str, search_item: dict[str, Any], details: dict[str, Any]) -> DiscoveryCandidate:
    instrument = details.get("instrument") if isinstance(details.get("instrument"), dict) else {}
    snapshot = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
    merged = {**search_item, **instrument, **details, **snapshot}
    epic = str(_value(merged, "epic") or "").strip()
    classification = classify_instrument(merged)
    status = _value(merged, "marketStatus")
    eligible = _eligible(merged, classification)
    verified = bool(epic and details and eligible and str(status or "").upper() == "TRADEABLE")
    safe_metadata = {
        key: merged[key]
        for key in (
            "name", "instrumentType", "marketStatus", "expiry", "lotSize", "contractSize",
            "streamingPricesAvailable", "type", "instrumentName", "marketId",
        )
        if key in merged and merged[key] not in (None, "")
    }
    instrument_type = _value(merged, "instrumentType", "type")
    expiry = _value(merged, "expiry", "expiration")
    return DiscoveryCandidate(
        requested_market,
        str(_value(merged, "name", "instrumentName", "marketName") or requested_market),
        epic,
        str(status) if status is not None else None,
        str(instrument_type) if instrument_type is not None else None,
        str(expiry) if expiry is not None else None,
        classification,
        eligible,
        verified,
        safe_metadata,
    )


def discover_market_groups(client: IGRestClient) -> list[MarketDiscovery]:
    groups: list[MarketDiscovery] = []
    for requested_market, terms in SEARCH_TERMS.items():
        by_epic: dict[str, DiscoveryCandidate] = {}
        for term in terms:
            for item in client.search_markets(term):
                epic = str(item.get("epic", "")).strip()
                if not epic or epic in by_epic:
                    continue
                # Search results are hints only. Detail verification is mandatory.
                details = client.market_details(epic)
                by_epic[epic] = _candidate(requested_market, item, details)
        candidates = tuple(by_epic.values())
        primary = tuple(candidate for candidate in candidates if candidate.eligible_primary)
        status = (
            "VERIFIED" if len(primary) == 1 and primary[0].verified
            else "AMBIGUOUS" if primary
            else "NOT FOUND"
        )
        groups.append(MarketDiscovery(requested_market, status, candidates))
    return groups


def discover_markets(
    client: IGRestClient, search_terms: dict[str, str] | None = None
) -> list[DiscoveryCandidate]:
    """Compatibility wrapper; custom terms remain useful for isolated tests."""
    if search_terms is None:
        return [candidate for group in discover_market_groups(client) for candidate in group.candidates]
    results: list[DiscoveryCandidate] = []
    for requested_market, term in search_terms.items():
        for item in client.search_markets(term):
            epic = str(item.get("epic", "")).strip()
            if epic:
                results.append(_candidate(requested_market, item, client.market_details(epic)))
    return results


def to_verified_instrument(candidate: DiscoveryCandidate) -> VerifiedInstrument:
    if not candidate.verified:
        raise ValueError("candidate is not provider-verified")
    return VerifiedInstrument(
        candidate.requested_market,
        candidate.market_name,
        candidate.epic,
        candidate.instrument_type,
        candidate.market_status,
        candidate.expiry,
        candidate.classification,
        candidate.metadata,
    )
