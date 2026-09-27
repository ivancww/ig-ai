from ig_ai.discovery import classify_instrument, discover_market_groups


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
    assert set(client.detail_epics) == {"SHARE", "OPTION", "FUT", "CASH"}


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


def test_no_detail_means_candidate_is_not_verified():
    client = FakeClient({"US Tech 100": [{"epic": "EPIC"}]}, {"EPIC": {}})
    group = discover_market_groups(client)[0]
    assert group.status == "NOT FOUND"
    assert not group.candidates[0].verified
