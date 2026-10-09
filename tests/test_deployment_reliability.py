import json
import sys
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ig_ai import cli, discovery
from ig_ai.database import Database
from ig_ai.exceptions import IGHTTPError
from ig_ai.runtime import PersistedStream
from ig_ai.runtime_service import RuntimeService, Schedule, run_live_service
from ig_ai.web import WebReadModel


class _ShutdownStream:
    def __init__(self):
        self.stop_event = threading.Event()
        self.stop_calls = []
        self.stats = SimpleNamespace(
            diagnostics=SimpleNamespace(exit_reason=None, duration_expired=False),
            last_update={},
        )

    def run(self, _callback):
        while not self.stop_event.wait(0.01):
            pass
        self.stats.diagnostics.exit_reason = "SHUTDOWN"

    def stop(self, *, reason="requested"):
        self.stop_calls.append(reason)
        self.stop_event.set()
        self.stats.diagnostics.exit_reason = "SHUTDOWN" if reason == "shutdown" else reason.upper()

    def mark_duration_expired(self):
        self.stats.diagnostics.duration_expired = True


class _DelayedShutdownStream(_ShutdownStream):
    def stop(self, *, reason="requested"):
        self.stop_calls.append(reason)
        self.stats.diagnostics.exit_reason = "SHUTDOWN"
        if len(self.stop_calls) > 1:
            self.stop_event.set()


def test_shutdown_event_exits_owner_loop_and_joins_stream_worker(tmp_path):
    database = Database(tmp_path / "shutdown.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start()
    stream = _ShutdownStream()
    sink = PersistedStream(database, {}, stop_requested=service.stop_requested)
    stopper = threading.Thread(target=lambda: (time.sleep(0.05), service.request_shutdown()), daemon=True)
    stopper.start()
    sink.run_for(stream, 3600)
    stopper.join(timeout=1)
    assert stream.stop_calls == ["shutdown"]
    assert sink.runtime_audit["worker_stopped"] is True
    database.close()


def test_delayed_transport_close_is_retried_within_a_bounded_shutdown_window(tmp_path):
    database = Database(tmp_path / "delayed-shutdown.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start()
    stream = _DelayedShutdownStream()
    sink = PersistedStream(database, {}, stop_requested=service.stop_requested)
    stopper = threading.Thread(target=lambda: (time.sleep(0.05), service.request_shutdown()), daemon=True)
    stopper.start()
    started = time.monotonic()
    sink.run_for(stream, 3600)
    elapsed = time.monotonic() - started
    assert elapsed < 7
    assert len(stream.stop_calls) == 2
    assert sink.runtime_audit["worker_stopped"] is True
    database.close()


def test_owner_loop_refuses_to_claim_success_if_worker_never_stops(tmp_path):
    class NeverStops(_ShutdownStream):
        def stop(self, *, reason="requested"):
            self.stop_calls.append(reason)
            self.stats.diagnostics.exit_reason = "SHUTDOWN"

    database = Database(tmp_path / "never-stops.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start()
    stream = NeverStops()
    sink = PersistedStream(database, {}, stop_requested=service.stop_requested)
    stopper = threading.Thread(target=lambda: (time.sleep(0.05), service.request_shutdown()), daemon=True)
    stopper.start()
    with pytest.raises(RuntimeError, match="bounded shutdown deadline"):
        sink.run_for(stream, 3600)
    assert len(stream.stop_calls) == 2
    database.close()


def test_reporting_permission_failure_never_masks_application_failure(monkeypatch):
    monkeypatch.setattr(cli.Settings, "from_env", lambda: (_ for _ in ()).throw(RuntimeError("original failure")))
    monkeypatch.setattr(cli, "update_terminal_report", lambda **_kwargs: (_ for _ in ()).throw(PermissionError("state denied")))
    monkeypatch.setattr(sys, "argv", ["ig-ai", "rest-check"])
    with pytest.raises(RuntimeError, match="original failure"):
        cli.main()


def test_new_runtime_generation_rejects_old_persisted_self_monitor_snapshot(tmp_path):
    database = Database(tmp_path / "generation.sqlite3")
    database.save_runtime_state("service", {
        "status": "HEALTHY", "runtime_generation": "old", "service_started_at": "2020-01-01T00:00:00+00:00",
        "last_heartbeat_at": "2020-01-01T00:00:00+00:00", "ig_connection": "CONNECTED",
    })
    database.save_runtime_state("self_monitoring", {
        "runtime_generation": "old", "generated_at": "2020-01-01T00:00:00+00:00", "overall_status": "HEALTHY",
        "service": {"status": "HEALTHY"}, "incidents": [],
    })
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start()
    health = WebReadModel(database).health()
    assert health["self_monitoring"]["ready"] is False
    assert health["self_monitoring"]["runtime_generation"] == service.runtime_generation
    assert health["overall_status"] == "DEGRADED"
    database.close()


def test_current_generation_requires_current_heartbeat_and_self_monitor_snapshot(tmp_path):
    database = Database(tmp_path / "current-generation.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start(["EPIC"])
    service.heartbeat(connection="CONNECTED", market_data={"EPIC": datetime.now(UTC)})
    service.update_market(
        "EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC),
        connection="CONNECTED", market_status="TRADEABLE", market_status_source="FAKE_IG",
    )
    service.evaluate_self_monitoring(force=True)
    health = WebReadModel(database).health()
    assert health["self_monitoring"]["ready"] is True
    assert health["self_monitoring"]["runtime_generation"] == service.runtime_generation
    database.close()


def test_current_generation_rejects_stale_heartbeat_even_when_snapshot_generation_matches(tmp_path):
    database = Database(tmp_path / "stale-current-generation.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start(["EPIC"])
    stale = datetime.now(UTC).replace(year=2020).isoformat()
    database.save_runtime_state("service", {
        "status": "HEALTHY", "runtime_generation": service.runtime_generation,
        "service_started_at": service.started_at.isoformat(), "last_heartbeat_at": stale,
        "ig_connection": "CONNECTED",
    })
    database.save_runtime_state("self_monitoring", {
        "runtime_generation": service.runtime_generation,
        "generated_at": service.started_at.isoformat(), "generation_status": "CURRENT",
        "overall_status": "HEALTHY", "service": {"heartbeat": stale}, "incidents": [],
    })
    health = WebReadModel(database).health()
    assert health["self_monitoring"]["ready"] is False
    assert health["overall_status"] == "DEGRADED"
    database.close()


def test_web_health_uses_current_runtime_connection_over_persisted_snapshot(tmp_path):
    database = Database(tmp_path / "runtime-overrides-snapshot.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start(["EPIC"])
    service.heartbeat(connection="CONNECTED", market_data={"EPIC": datetime.now(UTC)})
    service.update_market(
        "EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC),
        connection="CONNECTED", market_status="TRADEABLE", market_status_source="FAKE_IG",
    )
    service.evaluate_self_monitoring(force=True)
    service.update_connection("DISCONNECTED")
    health = WebReadModel(database).health()
    assert health["service"]["status"] == "DEGRADED"
    assert health["ig"]["connection_status"] == "DISCONNECTED"
    assert health["self_monitoring"]["ready"] is False
    database.close()


def test_http_403_is_categorized_without_claiming_provider_root_cause(tmp_path):
    database = Database(tmp_path / "403.sqlite3")
    service = RuntimeService(database)
    service.start()
    service.record_startup_failure(IGHTTPError(403, "request failed", endpoint="/markets", method="GET", phase="search"))
    state = database.get_runtime_state("service")
    assert state["startup_failure"]["category"] == "IG_HTTP_403"
    assert "credentials" not in json.dumps(state).lower()
    assert "root cause" not in json.dumps(state).lower()
    database.close()


def test_live_service_records_fake_provider_403_before_reporting_failure(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, _settings):
            pass

    def fail_discovery(_client):
        raise IGHTTPError(403, "request failed", endpoint="/markets", method="GET", phase="search")

    monkeypatch.setattr("ig_ai.rest.IGRestClient", FakeClient)
    monkeypatch.setattr(discovery, "discover_market_groups", fail_discovery)
    settings = SimpleNamespace(
        database_path=tmp_path / "live-403.sqlite3",
        service_heartbeat_seconds=1,
        stale_data_seconds=10,
    )
    with pytest.raises(IGHTTPError):
        run_live_service(settings)
    database = Database(settings.database_path)
    state = database.get_runtime_state("service")
    assert state["startup_failure"]["category"] == "IG_HTTP_403"
    assert state["status"] == "STOPPING"
    assert "credentials" not in json.dumps(state).lower()
    database.close()
