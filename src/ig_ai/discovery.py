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
LOCALIZED_ALIASES = {
    "US Tech 100": ("美國科技股100", "纳斯达克100", "NASDAQ"),
    "Japan 225": ("日本225", "日經225", "NIKKEI"),
    "Hong Kong HS50": ("香港HS50", "香港50", "HS50", "HONG KONG 50"),
}
DEFAULT_DETAIL_BUDGET = 3
ALLOWANCE_ERROR = "error.public-api.exceeded-api-key-allowance"
INDEX_TYPES = {"INDICES", "INDEX"}
REJECTED_TYPES = {
    "OPT_INDICES",
    "OPTIONS",
    "SHARES",
    "ETFS",
    "ETF",
    "KNOCKOUTS",
    "KNOCKOUT",
    "LEVERAGED",
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
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class MarketDiscovery:
    requested_market: str
    status: str
    candidates: tuple[DiscoveryCandidate, ...]
    detail_calls: int = 0
    detail_budget: int = DEFAULT_DETAIL_BUDGET
    ambiguity_reason: str | None = None

    @property
    def verified_variants(self) -> tuple[DiscoveryCandidate, ...]:
        """All provider-verified primary variants, without choosing an EPIC."""
        return tuple(candidate for candidate in self.candidates if candidate.verified)


def _value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


def classify_instrument(data: dict[str, Any]) -> str:
    """Classify only from provider fields; UNKNOWN is safer than an assumption."""
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    expiry = str(_value(data, "expiry", "expiration") or "").upper()
    name = str(_value(data, "name", "marketName", "instrumentName") or "").upper()
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


def _eligible(data: dict[str, Any], classification: str, requested_market: str | None = None) -> bool:
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    name = str(_value(data, "name", "marketName", "instrumentName") or "").upper()
    if _is_excluded_name(name) or _is_weekend_instrument(data) or (
        name and requested_market and not _matches_requested_identity(requested_market, name)
    ):
        return False
    return (
        instrument_type in {"INDICES", "INDEX"}
        and classification == "CASH/ROLLING CFD"
        and not any(term in name for term in ("OPTION", "ETF", "SHARE", "LEVERAGED"))
    )


def _is_excluded_name(name: str) -> bool:
    return any(
        term in name
        for term in (
            "WEEKEND",
            "週末",
            "周末",
            "SATURDAY",
            "SUNDAY",
            "OPTION",
            "ETF",
            "SHARE",
            "LEVERAGED",
            "KNOCKOUT",
            "KNOCK-OUT",
            " 2X",
            " 3X",
        )
    ) or name.startswith(("2X", "3X"))


def _is_weekend_instrument(data: dict[str, Any]) -> bool:
    name = str(_value(data, "name", "marketName", "instrumentName") or "").upper()
    if any(term in name for term in ("WEEKEND", "週末", "周末", "SATURDAY", "SUNDAY")):
        return True
    for key in ("classification", "marketClassification", "marketType", "marketSession"):
        value = str(data.get(key) or "").upper()
        if "WEEKEND" in value or "週末" in value or "周末" in value:
            return True
    return False


def _matches_requested_identity(requested_market: str, name: str) -> bool:
    if requested_market != "Hong Kong HS50":
        return True
    # HSTECH is a separate confirmation market and must never satisfy HS50.
    if any(term in name for term in (
        "HSTECH", "HS TECH", "HANG SENG TECH", "TECHNOLOGY", "H-SHARE", "H SHARES",
        "H-SHARES", "CHINA ENTERPRISE",
    )):
        return False
    return any(term in name for term in ("HS50", "HS 50", "HONG KONG 50", "HANG SENG 50"))


def _search_score(requested_market: str, data: dict[str, Any]) -> int | None:
    """Return a ranking score, or None when search metadata makes detail unsafe."""
    instrument_type = str(_value(data, "instrumentType", "type") or "").upper()
    name = str(_value(data, "name", "marketName", "instrumentName") or "").upper()
    if instrument_type in REJECTED_TYPES:
        return None
    if instrument_type and instrument_type not in INDEX_TYPES:
        return None
    if name and (
        _is_excluded_name(name)
        or _is_weekend_instrument(data)
        or not _matches_requested_identity(requested_market, name)
    ):
        return None

    aliases = (
        requested_market,
        *SEARCH_TERMS.get(requested_market, ()),
        *LOCALIZED_ALIASES.get(requested_market, ()),
    )
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
    expiry = str(_value(data, "expiry", "expiration") or "").upper()
    if expiry in {"DFB", "-", "NULL", "NONE"}:
        score += 20
    if _is_excluded_name(name):
        score -= 100
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
    eligible = _eligible(merged, classification, requested_market)
    verified = bool(epic and details and eligible)
    safe_metadata = {
        key: merged[key]
        for key in (
            "name", "instrumentType", "marketStatus", "expiry", "lotSize", "contractSize",
            "streamingPricesAvailable", "type", "instrumentName", "marketId",
            "currency", "currencyCode", "denomination", "contractSize", "lotSize",
            "valuePerPoint", "pointValue", "unit", "unitOfMeasure", "instrumentUnit",
            "dealingSize", "minDealSize", "maxDealSize",
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
        None if eligible else "not a normal weekday cash/rolling index instrument",
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
    elif (
        requested_market == "Hong Kong HS50"
        and len(verified) > 1
        and len(ranked) == detail_calls
        and _has_distinct_variant_metadata(verified)
    ):
        status = "VERIFIED_VARIANTS"
    elif len(verified) == 1 and len(ranked) == detail_calls:
        status = "VERIFIED"
    elif primary or (shortlisted and detail_calls >= detail_budget):
        status = "AMBIGUOUS"
    else:
        status = "NOT FOUND"
    ambiguity_reason = None
    if status == "AMBIGUOUS":
        if primary and not verified:
            ambiguity_reason = "eligible candidates need provider detail verification"
        elif len(verified) > 1:
            ambiguity_reason = "multiple structurally verified candidates remain"
        elif detail_calls < len(ranked):
            ambiguity_reason = "detail budget exhausted before all plausible candidates were checked"
    return MarketDiscovery(
        requested_market,
        status,
        candidates,
        detail_calls=detail_calls,
        detail_budget=detail_budget,
        ambiguity_reason=ambiguity_reason,
    )


def _has_distinct_variant_metadata(candidates: tuple[DiscoveryCandidate, ...]) -> bool:
    """Require provider denomination/size evidence before naming variants."""
    variant_fields = (
        "denomination", "currency", "currencyCode", "contractSize", "lotSize",
        "valuePerPoint", "pointValue", "unit", "unitOfMeasure", "instrumentUnit",
        "dealingSize",
    )
    signatures = {
        tuple(candidate.metadata.get(field) for field in variant_fields)
        for candidate in candidates
    }
    name_signals = sum(
        any(token in candidate.market_name.upper() for token in ("$", "HK$", "£", "€"))
        for candidate in candidates
    )
    return len(signatures) > 1 and (any(any(value is not None for value in signature) for signature in signatures) or name_signals > 1)


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


def select_stream_instruments(groups: list[MarketDiscovery]) -> list[DiscoveryCandidate]:
    """Choose one verified weekday cash instrument per requested market.

    Selection is based on provider metadata. In particular, HS50's $1 contract
    wins over the legitimate HK$10 variant when metadata exposes that choice.
    No EPIC is embedded in this policy.
    """
    selected: list[DiscoveryCandidate] = []
    for group in groups:
        candidates = list(group.verified_variants)
        if not candidates:
            continue
        if group.requested_market == "Hong Kong HS50":
            def hk_rank(candidate: DiscoveryCandidate) -> tuple[int, int, str]:
                metadata = {str(k).lower(): str(v).lower() for k, v in candidate.metadata.items()}
                name = candidate.market_name.lower()
                size_one = metadata.get("contractsize") in {"1", "1.0"} or metadata.get("lotsize") in {"1", "1.0"}
                dollar_one = "$1" in name and "hk$10" not in name
                return (0 if size_one or dollar_one else 1, 0 if metadata.get("currency") in {"usd", "us dollar"} else 1, candidate.epic)
            candidates.sort(key=hk_rank)
        else:
            candidates.sort(key=lambda candidate: candidate.epic)
        selected.append(candidates[0])
    return selected
