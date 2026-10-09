from ig_ai.discovery import (
    classify_instrument,
    discover_market_groups,
    extract_market_status,
    refresh_market_status,
    select_stream_instruments,
)
from ig_ai.exceptions import IGHTTPError


class FakeClient:
    def __init__(self, results, details):
        self.results = results
        self.details = details
        self.terms = []
        self.detail_epics = []

    def search_markets(self, term):
        self.terms.append(term)
        return self.results.get(term, [])

    def market_details(self, epic):
        self.detail_epics.append(epic)
        return self.details[epic]


def test_classification_distinguishes_rolling_futures_and_other_types():
    assert classify_instrument({"instrumentType": "INDICES", "expiry": "DFB"}) == "CASH/ROLLING CFD"
    assert classify_instrument({"instrumentType": "INDICES", "expiry": "DEC-26"}) == "FUTURES/FORWARD"
    assert classify_instrument({"instrumentType": "SHARES", "expiry": "-"}) == "OTHER"
    assert classify_instrument({"instrumentType": "INDICES"}) == "UNKNOWN"


def test_extract_market_status_reads_provider_market_detail_shapes():
    assert extract_market_status({"instrument": {"marketStatus": "tradeable"}}) == "TRADEABLE"
    assert extract_market_status({"snapshot": {"marketStatus": "closed"}}) == "CLOSED"
    assert extract_market_status({"marketStatus": "EDITS_ONLY"}) == "EDITS_ONLY"
    assert extract_market_status({"instrument": {"name": "US Tech 100"}}) is None


def test_refresh_market_status_uses_read_only_market_details():
    client = FakeClient({}, {"EPIC": {"snapshot": {"marketStatus": "TRADEABLE"}}})
    assert refresh_market_status(client, "EPIC") == "TRADEABLE"
    assert client.detail_epics == ["EPIC"]


def test_filters_options_and_shares_and_requires_detail_verification():
    results = {
        "US Tech 100": [
            {"epic": "SHARE", "name": "Tech Share", "instrumentType": "SHARES"},
            {"epic": "OPTION", "name": "US Tech 100 Option", "instrumentType": "OPT_INDICES"},
            {"epic": "FUT", "name": "US Tech 100", "instrumentType": "INDICES"},
            {"epic": "CASH", "name": "US Tech 100", "instrumentType": "INDICES"},
        ]
    }
    details = {
        "SHARE": {"instrument": {"instrumentType": "SHARES", "name": "Tech Share"}},
        "OPTION": {"instrument": {"instrumentType": "OPT_INDICES", "name": "US Tech 100 Option"}},
        "FUT": {"instrument": {"instrumentType": "INDICES", "name": "US Tech 100", "expiry": "DEC-26", "marketStatus": "TRADEABLE"}},
        "CASH": {"instrument": {"instrumentType": "INDICES", "name": "US Tech 100", "expiry": "DFB", "marketStatus": "TRADEABLE", "lotSize": 1}},
    }
    client = FakeClient(results, details)
    group = discover_market_groups(client)[0]
    assert group.status == "VERIFIED"
    primary = [candidate for candidate in group.candidates if candidate.eligible_primary]
    assert [candidate.epic for candidate in primary] == ["CASH"]
    assert {candidate.epic for candidate in group.candidates} == {"SHARE", "OPTION", "FUT", "CASH"}
    assert set(client.detail_epics) == {"FUT", "CASH"}


def test_multiple_plausible_candidates_are_ambiguous_and_hong_kong_variants_are_searched():
    results = {
        "Hong Kong 50": [{"epic": "HK1"}],
        "HS50": [{"epic": "HK2"}],
    }
    details = {
        "HK1": {"instrument": {"name": "Hong Kong 50", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
        "HK2": {"instrument": {"name": "Hong Kong 50 HK$10", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
    }
    client = FakeClient(results, details)
    group = discover_market_groups(client)[2]
    assert group.status == "AMBIGUOUS"
    assert {"Hong Kong HS50", "Hong Kong 50", "Hong Kong", "HS50", "Hang Seng"}.issubset(client.terms)


def test_hong_kong_cash_variants_are_verified_without_selecting_an_epic():
    results = {
        "Hong Kong 50": [
            {"epic": "HK1", "name": "Hong Kong 50 $1", "instrumentType": "INDICES"},
            {"epic": "HK10", "name": "Hong Kong 50 HK$10", "instrumentType": "INDICES"},
        ]
    }
    details = {
        "HK1": {"instrument": {"name": "Hong Kong 50 $1", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE", "currency": "USD", "contractSize": 1}},
        "HK10": {"instrument": {"name": "Hong Kong 50 HK$10", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE", "currency": "HKD", "contractSize": 10}},
    }
    client = FakeClient(results, details)
    group = discover_market_groups(client)[2]

    assert group.status == "VERIFIED_VARIANTS"
    assert {candidate.epic for candidate in group.verified_variants} == {"HK1", "HK10"}
    assert {candidate.metadata["contractSize"] for candidate in group.verified_variants} == {1, 10}
    assert {candidate.metadata["currency"] for candidate in group.verified_variants} == {"USD", "HKD"}


def test_hong_kong_hstech_weekend_and_futures_are_not_hs50_primary_candidates():
    results = {
        "Hong Kong 50": [
            {"epic": "TECH", "name": "Hong Kong HSTECH", "instrumentType": "INDICES"},
            {"epic": "WEEKEND", "name": "Hong Kong 50 Weekend", "instrumentType": "INDICES"},
            {"epic": "FUTURE", "name": "Hong Kong 50", "instrumentType": "INDICES"},
            {"epic": "CASH", "name": "Hong Kong 50", "instrumentType": "INDICES"},
        ]
    }
    details = {
        "TECH": {"instrument": {"name": "Hong Kong HSTECH", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
        "WEEKEND": {"instrument": {"name": "Hong Kong 50 Weekend", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
        "FUTURE": {"instrument": {"name": "Hong Kong 50", "instrumentType": "INDICES", "expiry": "DEC-26", "marketStatus": "TRADEABLE"}},
        "CASH": {"instrument": {"name": "Hong Kong 50", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
    }
    client = FakeClient(results, details)
    group = discover_market_groups(client)[2]

    assert [candidate.epic for candidate in group.candidates if candidate.eligible_primary] == ["CASH"]


def test_no_detail_means_candidate_is_not_verified():
    client = FakeClient({"US Tech 100": [{"epic": "EPIC"}]}, {"EPIC": {}})
    group = discover_market_groups(client)[0]
    assert group.status == "NOT FOUND"
    assert not group.candidates[0].verified


def test_irrelevant_search_candidates_do_not_trigger_detail_calls():
    results = {
        "US Tech 100": [
            {"epic": "FX", "name": "US Dollar / Yen", "instrumentType": "CURRENCIES"},
            {"epic": "ETF", "name": "US Tech 100 2X ETF", "instrumentType": "SHARES"},
            {"epic": "INDEX", "name": "US Tech 100", "instrumentType": "INDICES"},
        ]
    }
    details = {"INDEX": {"instrument": {"instrumentType": "INDICES", "name": "US Tech 100", "expiry": "DFB", "marketStatus": "TRADEABLE"}}}
    client = FakeClient(results, details)
    group = discover_market_groups(client, instrument_detail_budget=1)[0]
    assert client.detail_epics == ["INDEX"]
    assert {candidate.epic for candidate in group.candidates} == {"FX", "ETF", "INDEX"}


def test_detail_budget_is_strict_and_shortlists_indices():
    results = {
        "US Tech 100": [
            {"epic": "INDEX1", "name": "US Tech 100", "instrumentType": "INDICES"},
            {"epic": "INDEX2", "name": "US Tech 100", "instrumentType": "INDICES"},
            {"epic": "INDEX3", "name": "US Tech 100", "instrumentType": "INDICES"},
            {"epic": "INDEX4", "name": "US Tech 100", "instrumentType": "INDICES"},
        ]
    }
    details = {
        epic: {"instrument": {"instrumentType": "INDICES", "name": "US Tech 100", "expiry": "DEC-26"}}
        for epic in ("INDEX1", "INDEX2", "INDEX3", "INDEX4")
    }
    client = FakeClient(results, details)
    discover_market_groups(client, instrument_detail_budget=2)
    assert len(client.detail_epics) == 2
    assert set(client.detail_epics) == {"INDEX1", "INDEX2"}


def test_unique_verified_candidate_stops_further_detail_calls():
    results = {
        "US Tech 100": [
            {"epic": "CASH", "name": "US Tech 100", "instrumentType": "INDICES", "marketStatus": "TRADEABLE"},
        ]
    }
    details = {"CASH": {"instrument": {"instrumentType": "INDICES", "name": "US Tech 100", "expiry": "DFB", "marketStatus": "TRADEABLE"}}}
    client = FakeClient(results, details)
    group = discover_market_groups(client, instrument_detail_budget=3)[0]
    assert group.status == "VERIFIED"
    assert client.detail_epics == ["CASH"]


class AllowanceClient(FakeClient):
    def market_details(self, epic):
        self.detail_epics.append(epic)
        raise IGHTTPError(
            403,
            "request failed",
            provider_code="error.public-api.exceeded-api-key-allowance",
            endpoint=f"/markets/{epic}",
            method="GET",
            phase="instrument_details",
        )


def test_provider_allowance_stops_requests_and_reports_rate_limited():
    results = {
        "US Tech 100": [
            {"epic": "INDEX1", "name": "US Tech 100", "instrumentType": "INDICES"},
            {"epic": "INDEX2", "name": "US Tech 100", "instrumentType": "INDICES"},
        ]
    }
    client = AllowanceClient(results, {})
    group = discover_market_groups(client, instrument_detail_budget=3)[0]
    assert group.status == "RATE_LIMITED"
    assert client.detail_epics == ["INDEX1"]
    assert {candidate.epic for candidate in group.candidates} == {"INDEX1", "INDEX2"}


def test_live_us_tech_weekday_cash_beats_weekend_cash_and_status_is_structural():
    results = {"US Tech 100": [
        {"epic": "WEEKEND", "instrumentName": "週末美國科技股100指數 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-"},
        {"epic": "CASH", "instrumentName": "美國科技股100指數 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-"},
    ]}
    details = {
        "WEEKEND": {"instrument": {"instrumentName": "週末美國科技股100指數 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "TRADEABLE"}},
        "CASH": {"instrument": {"instrumentName": "美國科技股100指數 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "EDITS_ONLY"}},
    }
    group = discover_market_groups(FakeClient(results, details))[0]
    assert group.status == "VERIFIED"
    assert [candidate.epic for candidate in group.verified_variants] == ["CASH"]
    assert group.verified_variants[0].market_status == "EDITS_ONLY"


def test_live_japan_cash_beats_futures():
    results = {"Japan 225": [
        {"epic": "FUTURE", "instrumentName": "日本225", "instrumentType": "INDICES", "expiry": "26年12月"},
        {"epic": "CASH", "instrumentName": "日本225 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-"},
    ]}
    details = {
        "FUTURE": {"instrument": {"instrumentName": "日本225", "instrumentType": "INDICES", "expiry": "26年12月", "marketStatus": "TRADEABLE"}},
        "CASH": {"instrument": {"instrumentName": "日本225 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "CLOSED"}},
    }
    group = discover_market_groups(FakeClient(results, details))[1]
    assert group.status == "VERIFIED"
    assert [candidate.epic for candidate in group.verified_variants] == ["CASH"]


def test_hs50_cash_only_excludes_weekend_futures_hstech_h_shares_and_products():
    results = {"Hong Kong HS50": [
        {"epic": "CASH1", "instrumentName": "香港HS50 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-"},
        {"epic": "CASH10", "instrumentName": "香港HS50 現貨 (HK$10)", "instrumentType": "INDICES", "expiry": "-"},
        {"epic": "FUTURE", "instrumentName": "香港HS50 (HK$10)", "instrumentType": "INDICES", "expiry": "SEP-26"},
        {"epic": "WEEKEND", "instrumentName": "週末香港HS50 現貨 (HK$10)", "instrumentType": "INDICES", "expiry": "-"},
        {"epic": "TECH", "instrumentName": "香港HSTECH", "instrumentType": "INDICES", "expiry": "-"},
        {"epic": "SHARES", "instrumentName": "China H-shares", "instrumentType": "SHARES", "expiry": "-"},
        {"epic": "ETF", "instrumentName": "Hang Seng ETF", "instrumentType": "ETF", "expiry": "-"},
        {"epic": "KO", "instrumentName": "Hong Kong 50 Knockout", "instrumentType": "KNOCKOUTS", "expiry": "-"},
    ]}
    details = {
        "CASH1": {"instrument": {"instrumentName": "香港HS50 現貨 ($1)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "EDITS_ONLY", "currency": "USD", "lotSize": 1, "contractSize": 1, "unit": "CONTRACTS", "streamingPricesAvailable": True}},
        "CASH10": {"instrument": {"instrumentName": "香港HS50 現貨 (HK$10)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "CLOSED", "currency": "HKD", "lotSize": 10, "contractSize": 10, "unit": "CONTRACTS", "streamingPricesAvailable": True}},
        "FUTURE": {"instrument": {"instrumentName": "香港HS50 (HK$10)", "instrumentType": "INDICES", "expiry": "SEP-26", "marketStatus": "TRADEABLE"}},
        "WEEKEND": {"instrument": {"instrumentName": "週末香港HS50 現貨 (HK$10)", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "TRADEABLE"}},
        "TECH": {"instrument": {"instrumentName": "香港HSTECH", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "TRADEABLE"}},
    }
    group = discover_market_groups(FakeClient(results, details))[2]
    assert group.status == "VERIFIED_VARIANTS"
    assert {candidate.epic for candidate in group.verified_variants} == {"CASH1", "CASH10"}
    assert {candidate.metadata["currency"] for candidate in group.verified_variants} == {"USD", "HKD"}
    assert not any(candidate.verified for candidate in group.candidates if candidate.epic in {"FUTURE", "WEEKEND", "TECH"})


def test_plain_hang_seng_does_not_substitute_for_hs50():
    results = {"Hong Kong 50": [{"epic": "OTHER", "instrumentName": "Hang Seng China Enterprises", "instrumentType": "INDICES"}]}
    details = {"OTHER": {"instrument": {"instrumentName": "Hang Seng China Enterprises", "instrumentType": "INDICES", "expiry": "-", "marketStatus": "TRADEABLE"}}}
    group = discover_market_groups(FakeClient(results, details))[2]
    assert group.status == "NOT FOUND"


def test_stream_selection_prefers_hs50_dollar_one_variant():
    results = {
        "Hong Kong 50": [
            {"epic": "HK10", "name": "香港HS50 現貨 (HK$10)", "instrumentType": "INDICES"},
            {"epic": "HK1", "name": "香港HS50 現貨 ($1)", "instrumentType": "INDICES"},
        ]
    }
    details = {
        "HK10": {"instrument": {"name": "香港HS50 現貨 (HK$10)", "instrumentType": "INDICES", "expiry": "DFB", "contractSize": 10, "currency": "HKD"}},
        "HK1": {"instrument": {"name": "香港HS50 現貨 ($1)", "instrumentType": "INDICES", "expiry": "DFB", "contractSize": 1, "currency": "USD"}},
    }
    group = discover_market_groups(FakeClient(results, details))[2]
    assert select_stream_instruments([group])[0].epic == "HK1"
