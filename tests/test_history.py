import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ig_ai.database import Database
from ig_ai.exceptions import IGHTTPError, MalformedResponseError, RateLimitError
from ig_ai.history import (
    BackfillService,
    HistoricalIGClient,
    ReplayService,
    aggregate_candles,
    normalize_historical_price,
    report_gaps,
    safe_historical_response_diagnostic,
)
from ig_ai.models import Candle
from ig_ai.research import HistoricalReplay


def row(timestamp="2026-01-01T00:00:00Z"):
    return {
        "snapshotTimeUTC": timestamp,
        "openPrice": {"bid": 99, "ask": 101},
        "highPrice": {"bid": 104, "ask": 106},
        "lowPrice": {"bid": 94, "ask": 96},
        "closePrice": {"bid": 102, "ask": 104},
    }


def test_normalization_uses_midpoint_and_utc():
    candle = normalize_historical_price(row(), instrument_id="ig:EPIC", epic="EPIC", timeframe="15M")
    assert candle.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert candle.open == Decimal("100") and candle.close == Decimal("103")
    assert candle.volume is None and candle.is_closed


def test_invalid_ohlc_is_rejected():
    bad = row()
    bad["lowPrice"] = {"bid": 110, "ask": 111}
    with pytest.raises(ValueError):
        normalize_historical_price(bad, instrument_id="ig:EPIC", epic="EPIC", timeframe="1H")


def test_four_hour_aggregation_does_not_fabricate_partial_group():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [Candle("ig:E", "E", "1H", start + timedelta(hours=i), start + timedelta(hours=i + 1), Decimal(100 + i), Decimal(102 + i), Decimal(99 + i), Decimal(101 + i), is_closed=True) for i in range(4)]
    result = aggregate_candles(candles)
    assert len(result) == 1 and result[0].open == 100 and result[0].close == 104


def test_four_hour_aggregation_rejects_partial_and_non_contiguous_groups():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    partial = [Candle("ig:E", "E", "1H", start + timedelta(hours=i), start + timedelta(hours=i + 1), Decimal(100), Decimal(102), Decimal(99), Decimal(101), is_closed=True) for i in range(3)]
    non_contiguous = [*partial, Candle("ig:E", "E", "1H", start + timedelta(hours=5), start + timedelta(hours=6), Decimal(100), Decimal(102), Decimal(99), Decimal(101), is_closed=True)]
    assert aggregate_candles(partial) == []
    assert aggregate_candles(non_contiguous) == []


def test_v1_unzoned_snapshot_time_is_provider_utc():
    observed = {
        **row(),
        "snapshotTimeUTC": None,
        "snapshotTime": "2026/01/01 00:00:00",
    }
    candle = normalize_historical_price(observed, instrument_id="ig:E", epic="E", timeframe="1H")
    assert candle.start == datetime(2026, 1, 1, tzinfo=UTC)


def test_snapshot_time_utc_is_preferred_over_unzoned_snapshot_time():
    observed = {
        **row("2026-01-01T00:15:00Z"),
        "snapshotTime": "2026/01/01 00:00:00",
    }
    candle = normalize_historical_price(observed, instrument_id="ig:E", epic="E", timeframe="15M")
    assert candle.start == datetime(2026, 1, 1, 0, 15, tzinfo=UTC)


def test_unzoned_generic_timestamp_fallback_is_rejected():
    with pytest.raises(MalformedResponseError, match="explicit timezone"):
        normalize_historical_price({**row(), "snapshotTimeUTC": None, "timestamp": "2026-01-01 00:00:00"}, instrument_id="ig:E", epic="E", timeframe="1H")


@pytest.mark.parametrize(
    "value,allow_naive,expected",
    [
        ("2026-01-01T00:00:00Z", False, datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T01:00:00+01:00", False, datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026/01/01 00:00:00", True, datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_supported_ig_timestamp_shapes_are_normalized_to_utc(value, allow_naive, expected):
    from ig_ai.history import parse_ig_timestamp

    assert parse_ig_timestamp(value, allow_naive=allow_naive) == expected


def test_safe_historical_response_diagnostic_masks_values_and_exposes_shape_only():
    diagnostic = safe_historical_response_diagnostic(
        {
            "snapshotTime": "2026/01/01 00:00:00",
            "openPrice": {"bid": 1, "ask": 2},
            "closePrice": None,
        },
        {"pageData": {"pageNumber": 0, "totalPages": 1}, "allowance": {"remainingAllowance": 99}},
    )
    assert diagnostic["timestamp_fields"]["snapshotTime"] == {"type": "str", "length": 19, "masked_shape": "XXXX/XX/XX XX:XX:XX"}
    assert diagnostic["row_fields"] == {"snapshotTime": "str", "openPrice": "dict", "closePrice": "NoneType"}
    assert diagnostic["metadata_fields"] == {"allowance": "dict", "pageData": "dict"}
    assert diagnostic["paging_fields"]["pageData"] == {"pageNumber": "int", "totalPages": "int"}
    assert "2026" not in json.dumps(diagnostic)


class PagingClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_provider_paging_is_followed_and_rows_are_combined():
    first = {"prices": [row()], "metadata": {"paging": {"next": "/prices/E/HOUR?startdate=a&enddate=b"}}}
    second = {"prices": [{**row("2026-01-01T01:00:00Z")}], "metadata": {}}
    client = PagingClient([(first, {"X-REQUEST-ID": "safe"}), (second, {})])
    rows, metadata = HistoricalIGClient(client, pacing_seconds=0).fetch("E", "1H", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert len(rows) == 2 and metadata["pages"] == 2 and len(client.calls) == 2


def test_historical_date_range_uses_ig_version_one_query_contract():
    client = PagingClient([({"prices": [row()]}, {})])
    HistoricalIGClient(client, pacing_seconds=0).fetch(
        "CS.D.US.TECH.CFD.IP", "1H",
        datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 2, 0, 0, 2, tzinfo=UTC),
    )

    method, path, kwargs = client.calls[0]
    assert method == "GET"
    assert path == "/prices/CS.D.US.TECH.CFD.IP/HOUR"
    assert kwargs["params"] == {
        "startdate": "2026:01:01-00:00:01",
        "enddate": "2026:01:02-00:00:02",
    }
    assert kwargs["version"] == "1"
    assert kwargs["retry"] is False
    assert kwargs["phase"] == "historical_backfill"


def test_each_provider_page_gets_its_own_retry_budget(monkeypatch):
    first = {"prices": [row()], "metadata": {"paging": {"next": "/prices/E/HOUR?startdate=a&enddate=b"}}}
    second = {"prices": [{**row("2026-01-01T01:00:00Z")}], "metadata": {}}
    client = PagingClient([(first, {}), IGHTTPError(503, "temporary", retryable=True), (second, {})])
    monkeypatch.setattr("ig_ai.history.time.sleep", lambda _seconds: None)
    rows, metadata = HistoricalIGClient(client, pacing_seconds=0, max_retries=1).fetch("E", "1H", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert len(rows) == 2 and metadata["pages"] == 2 and len(client.calls) == 3


def test_provider_truncation_without_next_link_is_rejected():
    client = PagingClient([({"prices": [row()], "metadata": {"pageData": {"pageNumber": 0, "totalPages": 2}}}, {})])
    with pytest.raises(MalformedResponseError, match="truncated"):
        HistoricalIGClient(client, pacing_seconds=0).fetch("E", "1H", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


def test_transient_retry_is_bounded_and_permanent_error_is_not_retried_forever(monkeypatch):
    class RetryClient:
        def __init__(self, failures):
            self.failures = failures
            self.calls = 0

        def _request(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise IGHTTPError(503, "server failure", retryable=True)
            return ({"prices": [row()]}, {})

    monkeypatch.setattr("ig_ai.history.time.sleep", lambda _seconds: None)
    transient = RetryClient(2)
    HistoricalIGClient(transient, pacing_seconds=0, max_retries=2).fetch("E", "1H", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert transient.calls == 3
    permanent = RetryClient(10)
    with pytest.raises(IGHTTPError):
        HistoricalIGClient(permanent, pacing_seconds=0, max_retries=2).fetch("E", "1H", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert permanent.calls == 3


def test_gap_report_is_explicit():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = Candle("ig:E", "E", "1H", start, start + timedelta(hours=1), Decimal(1), Decimal(2), Decimal(0), Decimal(1), is_closed=True)
    third = Candle("ig:E", "E", "1H", start + timedelta(hours=3), start + timedelta(hours=4), Decimal(1), Decimal(2), Decimal(0), Decimal(1), is_closed=True)
    report = report_gaps([first, third])
    assert report.missing_intervals == 1


def test_historical_conflict_is_idempotent_and_does_not_replace_live(tmp_path):
    database = Database(tmp_path / "history.sqlite3")
    candle = normalize_historical_price(row(), instrument_id="ig:E", epic="E", timeframe="15M")
    database.save_candle(candle)
    assert database.save_historical_candle(candle, provenance={"provider": "IG"}) == "SKIPPED_LIVE_CONFLICT"
    historical = normalize_historical_price(row(), instrument_id="ig:H", epic="H", timeframe="15M")
    assert database.save_historical_candle(historical, provenance={"provider": "IG", "CST": "secret", "api_key": "secret"}) == "INSERTED"
    provenance_text = database.connection.execute("SELECT provenance_json FROM candle_provenance WHERE instrument_id='ig:H'").fetchone()[0]
    assert "secret" not in provenance_text and "CST" not in provenance_text
    live = Candle("ig:H", "H", "15M", historical.start, historical.end, Decimal(200), Decimal(202), Decimal(199), Decimal(201), is_closed=True)
    database.save_candle(live)
    assert database.connection.execute("SELECT source, close FROM candle_provenance JOIN candles USING (instrument_id, timeframe, start_at) WHERE instrument_id='ig:H'").fetchone() == ("LIVE_AGGREGATED", "201")
    database.close()


def test_dry_run_adapter_does_not_require_persistence(tmp_path):
    class Client:
        def search_markets(self, term):
            return [{"epic": "EPIC", "name": term, "instrumentType": "INDICES", "expiry": "DFB"}]

        def market_details(self, epic):
            return {"instrument": {"name": "US Tech 100", "type": "INDICES", "expiry": "DFB"}, "snapshot": {"marketStatus": "TRADEABLE"}}

        def ensure_session(self):
            return object()

    class Wrapper:
        client = Client()

        def search_markets(self, term):
            return self.client.search_markets(term)

        def market_details(self, epic):
            return self.client.market_details(epic)

    from ig_ai.history import BackfillService
    database = Database(tmp_path / "dry.sqlite3")
    result = BackfillService(database, HistoricalIGClient(Wrapper(), pacing_seconds=0)).run(market="US Tech 100", timeframe="1H", start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 2, tzinfo=UTC), dry_run=True)
    assert result["status"] == "DRY_RUN"
    assert database.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0
    database.close()


def test_rate_limit_pause_and_resume_checkpoint(tmp_path):
    class Candidate:
        epic = "E"
        market_name = "US Tech 100"
        instrument_type = "INDICES"
        market_status = "CLOSED"
        metadata = {}
        classification = "CASH/ROLLING CFD"

    class Provider:
        client = object()
        pacing_seconds = 0

        def __init__(self):
            self.paused = True

        def fetch(self, *args):
            if self.paused:
                raise RateLimitError(429, "rate limit", retryable=True)
            return [row()], {"complete": True, "pages": 1}

    class Service(BackfillService):
        def resolve(self, _market):
            return Candidate()

    provider = Provider()
    database = Database(tmp_path / "pause.sqlite3")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = Service(database, provider).run(market="US Tech 100", timeframe="1H", start=start, end=start + timedelta(days=1))
    assert first["status"] == "BACKFILL_PAUSED_RATE_LIMIT"
    provider.paused = False
    second = Service(database, provider).run(market="US Tech 100", timeframe="1H", start=start, end=start + timedelta(days=1), resume=True)
    assert second["status"] == "COMPLETE"
    assert database.connection.execute("SELECT status FROM backfill_jobs").fetchone()[0] == "COMPLETE"
    database.close()


def test_replay_is_bounded_and_resumes_after_interruption(tmp_path, monkeypatch):
    database = Database(tmp_path / "replay.sqlite3")
    database.save_instrument("ig:E", "E", "US Tech 100", instrument_type="INDICES")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(4):
        candle = Candle("ig:E", "E", "1H", start + timedelta(hours=index), start + timedelta(hours=index + 1), Decimal(100), Decimal(102), Decimal(99), Decimal(101 + index), is_closed=True)
        database.save_candle(candle)
    original_list = database.list_candles
    database.list_candles = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("replay must use bounded iterator"))
    original_checkpoint = database.checkpoint_replay_job
    calls = {"count": 0}

    def interrupt_once(*args, **kwargs):
        original_checkpoint(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(database, "checkpoint_replay_job", interrupt_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ReplayService(database, max_history=2).run(instrument_id="ig:E", start=start, end=start + timedelta(hours=4))
    monkeypatch.setattr(database, "checkpoint_replay_job", original_checkpoint)
    result = ReplayService(database, max_history=2).run(instrument_id="ig:E", start=start, end=start + timedelta(hours=4))
    assert result["snapshots"] == 3
    assert database.connection.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0] == 4
    database.list_candles = original_list
    database.close()


def test_replay_excludes_forming_and_future_context_on_all_timeframes():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    target = Candle("ig:E", "E", "1H", start, start + timedelta(hours=1), Decimal(100), Decimal(102), Decimal(99), Decimal(101), is_closed=True)
    future_15m = Candle("ig:E", "E", "15M", start + timedelta(hours=1), start + timedelta(hours=1, minutes=15), Decimal(1000), Decimal(1002), Decimal(999), Decimal(1001), is_closed=True)
    forming_4h = Candle("ig:E", "E", "4H", start, start + timedelta(hours=4), Decimal(1000), Decimal(1002), Decimal(999), Decimal(1001), is_closed=False)
    future_1d = Candle("ig:E", "E", "1D", start, start + timedelta(days=1), Decimal(1000), Decimal(1002), Decimal(999), Decimal(1001), is_closed=True)
    result = HistoricalReplay(max_history=20).direction_states({"1H": [target], "15M": [future_15m], "4H": [forming_4h], "1D": [future_1d]})
    assert result[0]["histories"]["15M"] == []
    assert result[0]["histories"]["4H"] == []
    assert result[0]["histories"]["1D"] == []
    assert result[0]["state"]["model_reference"]["information_time"] == (start + timedelta(hours=1)).isoformat()


def test_future_15m_is_outcome_only_and_cannot_change_frozen_model_state(tmp_path):
    def run_case(path, extreme):
        database = Database(path)
        database.save_instrument("ig:E", "E", "US Tech 100", instrument_type="INDICES")
        start = datetime(2026, 1, 1, tzinfo=UTC)
        database.save_candle(Candle("ig:E", "E", "1H", start, start + timedelta(hours=1), Decimal(100), Decimal(102), Decimal(99), Decimal(101), is_closed=True))
        for index in range(4):
            point = Decimal("10000") if extreme else Decimal("100")
            future_start = start + timedelta(hours=1, minutes=15 * index)
            database.save_candle(Candle("ig:E", "E", "15M", future_start, future_start + timedelta(minutes=15), point, point + 2, point - 1, point + 1, is_closed=True))
        ReplayService(database).run(instrument_id="ig:E", start=start, end=start + timedelta(hours=1))
        context = database.connection.execute("SELECT context_json FROM research_snapshots").fetchone()[0]
        outcome = database.connection.execute("SELECT status FROM research_outcomes WHERE horizon='1H'").fetchone()[0]
        database.close()
        return context, outcome

    ordinary_context, ordinary_outcome = run_case(tmp_path / "ordinary.sqlite3", False)
    extreme_context, extreme_outcome = run_case(tmp_path / "extreme.sqlite3", True)
    assert json.loads(ordinary_context) == json.loads(extreme_context)
    assert ordinary_outcome == "COMPLETE" and extreme_outcome == "COMPLETE"
