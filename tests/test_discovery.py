from ig_ai.discovery import classify_instrument, discover_market_groups
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
        "HK2": {"instrument": {"name": "Hang Seng", "instrumentType": "INDICES", "expiry": "DFB", "marketStatus": "TRADEABLE"}},
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
