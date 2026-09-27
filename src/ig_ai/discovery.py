from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .exceptions import IGHTTPError, RateLimitError
from .models import VerifiedInstrument
from .rest import IGRestClient

SEARCH_TERMS = {
    "US Tech 100": ("US Tech 100", "Nasdaq 100", "US Tech"),
    "Japan 225": ("Japan 225", "Japan 225 Index", "Nikkei 225"),
    "Hong Kong HS50": ("Hong Kong HS50", "Hong Kong 50", "Hong Kong", "HS50", "Hang Seng"),
}
DEFAULT_DETAIL_BUDGET = 3
ALLOWANCE_ERROR = "error.public-api.exceeded-api-key-allowance"
INDEX_TYPES = {"INDICES", "INDEX"}
REJECTED_TYPES = {"OPT_INDICES", "OPTIONS", "SHARES"}


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


def _search_score(requested_market: str, data: dict[str, Any]) -> int | None:
    """Return a ranking score, or None when search metadata makes detail unsafe."""
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    name = str(_value(data, "name", "marketName", "instrumentName") or "").upper()
    if instrument_type in REJECTED_TYPES:
        return None
    if instrument_type and instrument_type not in INDEX_TYPES:
        return None
    if (
        any(term in name for term in ("OPTION", "ETF", "SHARE", "LEVERAGED", " 2X", " 3X"))
        or name.startswith(("2X", "3X"))
    ):
        return None

    aliases = (requested_market, *SEARCH_TERMS.get(requested_market, ()))
    name_match = any(alias.upper() in name for alias in aliases if alias)
    # A named, unrelated result is not a plausible primary index. An entirely
    # sparse provider result remains eligible for ranking for compatibility, but
    # never becomes verified without the detail response.
    if name and not name_match:
        return None
    score = 0
    if instrument_type in INDEX_TYPES:
        score += 100
    if name_match:
        score += 50
    if str(_value(data, "marketStatus", "status") or "").upper() == "TRADEABLE":
        score += 10
    if str(_value(data, "expiry", "expiration") or "").upper() == "DFB":
        score += 5
    return score


def _candidate(
    requested_market: str, search_item: dict[str, Any], details: dict[str, Any] | None = None
) -> DiscoveryCandidate:
    details = details or {}
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


def _discover_group(
    client: IGRestClient,
    requested_market: str,
    terms: tuple[str, ...],
    detail_budget: int,
) -> MarketDiscovery:
    raw_by_epic: dict[str, tuple[int, dict[str, Any]]] = {}
    all_by_epic: dict[str, dict[str, Any]] = {}
    for term in terms:
        for item in client.search_markets(term):
            epic = str(item.get("epic", "")).strip()
            if not epic:
                continue
            if epic not in all_by_epic or len(item) > len(all_by_epic[epic]):
                all_by_epic[epic] = item
            score = _search_score(requested_market, item)
            if score is None:
                continue
            previous = raw_by_epic.get(epic)
            if previous is None or score > previous[0] or len(item) > len(previous[1]):
                raw_by_epic[epic] = (score, item)

    ranked = sorted(raw_by_epic.values(), key=lambda value: value[0], reverse=True)
    shortlisted = ranked[:detail_budget]
    by_epic = {epic: _candidate(requested_market, item) for epic, item in all_by_epic.items()}
    detail_calls = 0
    rate_limited = False
    for _, search_item in shortlisted:
        if detail_calls >= detail_budget:
            break
        epic = str(search_item["epic"]).strip()
        try:
            details = client.market_details(epic)
        except (RateLimitError, IGHTTPError) as error:
            if isinstance(error, RateLimitError) or error.provider_code == ALLOWANCE_ERROR:
                rate_limited = True
                break
            raise
        detail_calls += 1
        by_epic[epic] = _candidate(requested_market, search_item, details)
        candidate = by_epic[epic]
        # A sole shortlisted candidate that is already verified needs no more
        # detail requests. Other plausible candidates keep the result ambiguous.
        if candidate.verified and len(shortlisted) == 1:
            break

    candidates = tuple(by_epic.values())
    primary = tuple(candidate for candidate in candidates if candidate.eligible_primary)
    verified = tuple(candidate for candidate in primary if candidate.verified)
    if rate_limited:
        status = "RATE_LIMITED"
    elif len(verified) == 1 and len(ranked) == detail_calls:
        status = "VERIFIED"
    elif primary or (shortlisted and detail_calls >= detail_budget):
        status = "AMBIGUOUS"
    else:
        status = "NOT FOUND"
    return MarketDiscovery(requested_market, status, candidates)


def _configured_detail_budget(client: IGRestClient, override: int | None) -> int:
    budget = override if override is not None else getattr(
        getattr(client, "settings", None), "discovery_detail_budget", DEFAULT_DETAIL_BUDGET
    )
    if not isinstance(budget, int) or budget <= 0:
        raise ValueError("instrument detail request budget must be a positive integer")
    return budget


def discover_market_groups(
    client: IGRestClient, *, instrument_detail_budget: int | None = None
) -> list[MarketDiscovery]:
    detail_budget = _configured_detail_budget(client, instrument_detail_budget)
    groups: list[MarketDiscovery] = []
    for requested_market, terms in SEARCH_TERMS.items():
        group = _discover_group(client, requested_market, terms, detail_budget)
        groups.append(group)
        if group.status == "RATE_LIMITED":
            break
    return groups


def discover_markets(
    client: IGRestClient,
    search_terms: dict[str, str] | None = None,
    *,
    instrument_detail_budget: int | None = None,
) -> list[DiscoveryCandidate]:
    """Compatibility wrapper; custom terms remain useful for isolated tests."""
    if search_terms is None:
        return [
            candidate
            for group in discover_market_groups(
                client, instrument_detail_budget=instrument_detail_budget
            )
            for candidate in group.candidates
        ]
    detail_budget = _configured_detail_budget(client, instrument_detail_budget)
    results: list[DiscoveryCandidate] = []
    for requested_market, term in search_terms.items():
        group = _discover_group(client, requested_market, (term,), detail_budget)
        results.extend(group.candidates)
        if group.status == "RATE_LIMITED":
            break
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
