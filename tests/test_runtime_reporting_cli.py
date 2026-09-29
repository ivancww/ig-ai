from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ig_ai import cli, reporting


def _settings(tmp_path):
    return SimpleNamespace(
        account_type="LIVE",
        database_path=tmp_path / "runtime.sqlite3",
        api_key="api-key",
        username="username",
        password="password",
    )


def test_research_provenance_pass_requires_a_valid_persisted_identity():
    class Rows:
        def __init__(self, values):
            self.values = values

        def fetchall(self):
            return self.values

    class Connection:
        def execute(self, _query, _params):
            return Rows([("ig_cfd|instrument=ig:E|epic=E|market=US Tech 100|type=INDICES",)])

    database = SimpleNamespace(connection=Connection())
    assert cli.research_provenance_status(database, ["ig:E"]).startswith("PASS:")

    invalid = SimpleNamespace(connection=SimpleNamespace(execute=lambda _query, _params: Rows([("unknown",)])))
    assert cli.research_provenance_status(invalid, ["ig:E"]) == "NOT VERIFIED"


class _Database:
    def __init__(self, _path):
        pass

    def close(self):
        pass


def test_history_backfill_success_updates_runtime_report(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class HistoricalClient:
        request_count = 1

        def __init__(self, *_args, **_kwargs):
            self.request_count = 1

    class Service:
        def __init__(self, *_args):
            pass

        def run(self, **_kwargs):
            return {
                "status": "COMPLETE",
                "market": "US Tech 100",
                "epic": "IX.D.NASDAQ.IFMM.IP",
                "timeframe": "1H",
                "requested_start": "2026-08-30T00:00:00+00:00",
                "requested_end": "2026-09-29T00:00:00+00:00",
                "retrieved": 500,
                "inserted": 500,
                "skipped": 0,
                "available_range": {
                    "first": "2026-08-31T06:00:00+00:00",
                    "last": "2026-09-28T23:00:00+00:00",
                    "candle_count": 500,
                },
            }

    monkeypatch.setattr(reporting, "STATE_DIR", tmp_path / ".igai")
    monkeypatch.setattr(reporting, "UNIFIED_REPORT", tmp_path / "igai-report.txt")
    monkeypatch.setattr(cli.Settings, "from_env", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Database", _Database)
    monkeypatch.setattr(cli, "IGRestClient", lambda _settings: object())
    monkeypatch.setattr(cli, "HistoricalIGClient", HistoricalClient)
    monkeypatch.setattr(cli, "BackfillService", Service)
    monkeypatch.setattr(sys, "argv", ["ig-ai", "history-backfill", "--market", "US Tech 100", "--days", "30"])

    assert cli.main() == 0
    text = (tmp_path / "igai-report.txt").read_text()
    assert "Command executed: ig-ai history-backfill" in text
    assert "EPIC: IX.D.NASDAQ.IFMM.IP" in text
    assert "retrieved: 500" in text
    assert "historical request issued: YES" in text
    assert "IG_HISTORICAL / IG / CFD" in text


def test_history_backfill_pre_request_failure_is_current_and_safe(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class HistoricalClient:
        request_count = 0

        def __init__(self, *_args, **_kwargs):
            self.request_count = 0

    class Service:
        def __init__(self, *_args):
            pass

        def run(self, **_kwargs):
            raise RuntimeError("network failure")

    monkeypatch.setattr(reporting, "STATE_DIR", tmp_path / ".igai")
    monkeypatch.setattr(reporting, "UNIFIED_REPORT", tmp_path / "igai-report.txt")
    monkeypatch.setattr(cli.Settings, "from_env", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Database", _Database)
    monkeypatch.setattr(cli, "IGRestClient", lambda _settings: object())
    monkeypatch.setattr(cli, "HistoricalIGClient", HistoricalClient)
    monkeypatch.setattr(cli, "BackfillService", Service)
    monkeypatch.setattr(sys, "argv", ["ig-ai", "history-backfill", "--market", "US Tech 100", "--days", "30"])

    with pytest.raises(RuntimeError):
        cli.main()
    text = (tmp_path / "igai-report.txt").read_text()
    assert "Command executed: ig-ai history-backfill" in text
    assert "stage reached: historical backfill for US Tech 100" in text
    assert "historical request issued: NO" in text
    assert "safe failure category: RuntimeError" in text


def test_history_backfill_dry_run_does_not_claim_authentication_or_provider_access(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class HistoricalClient:
        request_count = 0

        def __init__(self, *_args, **_kwargs):
            self.request_count = 0

    class Service:
        def __init__(self, *_args):
            pass

        def run(self, **kwargs):
            assert kwargs["dry_run"] is True
            return {
                "status": "DRY_RUN",
                "market": "US Tech 100",
                "epic": "IX.D.NASDAQ.IFMM.IP",
                "timeframe": "1H",
                "chunks": [("2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00")],
            }

    monkeypatch.setattr(reporting, "STATE_DIR", tmp_path / ".igai")
    monkeypatch.setattr(reporting, "UNIFIED_REPORT", tmp_path / "igai-report.txt")
    monkeypatch.setattr(cli.Settings, "from_env", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Database", _Database)
    monkeypatch.setattr(cli, "IGRestClient", lambda _settings: object())
    monkeypatch.setattr(cli, "HistoricalIGClient", HistoricalClient)
    monkeypatch.setattr(cli, "BackfillService", Service)
    monkeypatch.setattr(sys, "argv", [
        "ig-ai", "history-backfill", "--market", "US Tech 100", "--days", "1", "--dry-run",
    ])

    assert cli.main() == 0
    text = (tmp_path / "igai-report.txt").read_text()
    assert "Authentication: NOT RUN (dry run)" in text
    assert "Final state: HISTORICAL BACKFILL DRY RUN" in text
    assert "historical request issued: NO" in text
    assert "provenance: NOT APPLICABLE (dry run)" in text


def test_replay_and_research_results_update_runtime_report(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class Database(_Database):
        def list_instruments(self):
            return [("ig:E", "E", "US Tech 100", "INDICES", "TRADEABLE", "{}")]

        def research_4h_comparison(self, **_kwargs):
            return {}

    class Replay:
        def __init__(self, *_args):
            pass

        def run(self, **_kwargs):
            return {"status": "COMPLETE", "processed": 3, "snapshots": 3, "outcomes": 2}

    class Research:
        def __init__(self, *_args):
            pass

        def run_direction_research(self, **_kwargs):
            return {"recorded_snapshots": 3, "outcomes_written": 2, "available_history": {"days": 30}}

        def run_pattern_research(self, **_kwargs):
            return {"recorded_snapshots": 0, "outcomes_written": 0}

        def run_alert_research(self, **_kwargs):
            return {"recorded_snapshots": 0, "outcomes_written": 0}

        def summary(self, **_kwargs):
            return {
                "research_version": "research_engine_v1",
                "as_of": "2026-09-29T00:00:00+00:00",
                "sample_count": 3,
                "snapshot_count": 3,
                "outcome_observation_count": 2,
                "quality": "READY",
                "rows": [{
                    "horizon": "1H", "sample_count": 2, "up_percentage": 50,
                    "down_percentage": 50, "flat_percentage": 0, "average_move": 0,
                    "median_mfe": 0, "median_mae": 0,
                }],
            }

        def research_4h_comparison(self, **_kwargs):
            return {}

    monkeypatch.setattr(reporting, "STATE_DIR", tmp_path / ".igai")
    monkeypatch.setattr(reporting, "UNIFIED_REPORT", tmp_path / "igai-report.txt")
    monkeypatch.setattr(cli.Settings, "from_env", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Database", Database)
    monkeypatch.setattr(cli, "ReplayService", Replay)
    monkeypatch.setattr(cli, "ResearchEngine", Research)

    monkeypatch.setattr(sys, "argv", [
        "ig-ai", "history-replay", "--market", "US Tech 100",
        "--start", "2026-09-01T00:00:00+00:00", "--end", "2026-09-02T00:00:00+00:00",
    ])
    assert cli.main() == 0
    text = (tmp_path / "igai-report.txt").read_text()
    assert "Command executed: ig-ai history-replay" in text
    assert "candidate snapshots: US Tech 100=3" in text
    assert "anti-lookahead: NOT INDEPENDENTLY RE-VERIFIED" in text
    assert "provenance: NOT OBSERVED" in text

    monkeypatch.setattr(sys, "argv", ["ig-ai", "research", "--instrument", "ig:E"])
    assert cli.main() == 0
    text = (tmp_path / "igai-report.txt").read_text()
    assert "Command executed: ig-ai research" in text
    assert "completed outcome counts by horizon: 1H=2" in text
    assert "replay: NOT RUN (research command does not execute Historical Replay)" in text
    assert "provenance: NOT OBSERVED" in text
