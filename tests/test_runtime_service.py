from datetime import UTC, datetime, timedelta

import pytest

from ig_ai.config import Settings
from ig_ai.database import Database
from ig_ai.exceptions import ConfigurationError
from ig_ai.runtime_service import RuntimeService, Schedule, format_service_status


def test_service_start_heartbeat_and_graceful_shutdown_persist_state(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start(["EPIC"])
    assert database.get_runtime_state("service")["status"] == "STARTING"
    service.heartbeat(connection="CONNECTED")
    assert database.get_runtime_state("service")["status"] == "HEALTHY"
    assert database.get_runtime_state("service").get("last_heartbeat_at")
    service.request_shutdown()
    assert service.stop_requested.is_set()
    service.stop()
    assert database.get_runtime_state("service")["status"] == "STOPPING"
    database.close()


def test_restart_restores_runtime_market_state_and_status_is_safe(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = Database(path)
    first.save_runtime_market_state("EPIC", {"instrument_id": "EPIC", "monitoring_state": "MONITORING"})
    first.save_runtime_schedule("EPIC", Schedule("CUSTOM", ("5 20:00-05:00",), "UTC").as_dict())
    first.close()
    reopened = Database(path)
    text = format_service_status({"service": {}, "markets": reopened.list_runtime_market_states(), "schedules": reopened.list_runtime_schedules()})
    assert "EPIC:" in text
    assert "CUSTOM" in text and "5 20:00-05:00" in text
    assert "Last Decision: NOT AVAILABLE" in text
    reopened.close()


def test_schedule_modes_and_unknown_market_hours():
    assert Schedule("24_7").is_active() is True
    custom = Schedule("CUSTOM", ("1,2,3,4,5 09:00-17:00",), "UTC")
    monday = datetime(2026, 10, 5, 10, tzinfo=UTC)
    sunday = datetime(2026, 10, 4, 10, tzinfo=UTC)
    assert custom.is_active(monday) is True
    assert custom.is_active(sunday) is False
    assert Schedule("MARKET_HOURS").is_active(monday) is None


def test_custom_schedule_overnight_weekday_semantics_and_timezone_conversion():
    overnight = Schedule("CUSTOM", ("5 20:00-05:00",), "UTC")
    assert overnight.is_active(datetime(2026, 10, 9, 21, tzinfo=UTC)) is True  # Friday
    assert overnight.is_active(datetime(2026, 10, 10, 1, tzinfo=UTC)) is True  # Saturday
    assert overnight.is_active(datetime(2026, 10, 10, 5, tzinfo=UTC)) is False
    normal = Schedule("CUSTOM", ("1,3 09:00-17:00",), "UTC")
    assert normal.is_active(datetime(2026, 10, 5, 16, tzinfo=UTC)) is True
    assert normal.is_active(datetime(2026, 10, 6, 10, tzinfo=UTC)) is False
    hong_kong = Schedule("CUSTOM", ("1 09:00-10:00",), "Asia/Hong_Kong")
    assert hong_kong.is_active(datetime(2026, 10, 5, 1, tzinfo=UTC)) is True


def test_custom_windows_configuration_is_validated_and_external():
    base = {"IG_API_KEY": "key", "IG_USERNAME": "user", "IG_PASSWORD": "pass", "IGAI_MONITORING_MODE": "CUSTOM"}
    settings = Settings.from_env({**base, "IGAI_CUSTOM_WINDOWS": "1,2,3,4,5 20:00-05:00;6 10:00-12:00"})
    assert settings.custom_windows == ("1,2,3,4,5 20:00-05:00", "6 10:00-12:00")
    for value in ("", "1,8 20:00-05:00", "1 25:00-05:00", "1 20:00-05:00;"):
        with pytest.raises(ConfigurationError):
            Settings.from_env({**base, "IGAI_CUSTOM_WINDOWS": value})


def test_market_stale_and_scheduled_off_are_distinct(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    service = RuntimeService(database, stale_seconds=60)
    service.start(["EPIC"])
    old = datetime.now(UTC) - timedelta(minutes=5)
    assert service.update_market("EPIC", schedule=Schedule("24_7"), last_data=old, market_status="TRADEABLE")["monitoring_state"] == "STALE_DATA"
    assert service.update_market("EPIC", schedule=Schedule("24_7"), last_data=old)["monitoring_state"] == "UNKNOWN"
    assert service.update_market("EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC), market_status="CLOSED")["monitoring_state"] == "MARKET_CLOSED"
    assert service.update_market("EPIC", schedule=Schedule("CUSTOM", ("1,2,3,4,5 00:00-00:01",)), last_data=datetime.now(UTC))["monitoring_state"] == "SCHEDULED_OFF"
    assert service.update_market("EPIC", schedule=Schedule("MARKET_HOURS"), last_data=datetime.now(UTC))["monitoring_state"] == "UNKNOWN"
    assert service.update_market("EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC), market_status="TRADEABLE")["monitoring_state"] == "MONITORING"
    database.close()


def test_market_status_provenance_is_persisted_and_exposed(tmp_path):
    database = Database(tmp_path / "market-status.sqlite3")
    service = RuntimeService(database)
    service.start(["EPIC"])
    refreshed_at = datetime.now(UTC)
    state = service.update_market(
        "EPIC",
        schedule=Schedule("24_7"),
        last_data=datetime.now(UTC),
        connection="CONNECTED",
        market_status="CLOSED",
        market_status_source="IG_MARKET_DETAILS",
        market_status_at=refreshed_at,
    )
    assert state["market_status"] == "CLOSED"
    assert state["market_status_source"] == "IG_MARKET_DETAILS"
    assert state["market_status_at"] == refreshed_at.isoformat()
    assert database.list_runtime_market_states()[0]["monitoring_state"] == "MARKET_CLOSED"
    database.close()


def test_connection_loss_degrades_and_reconnect_recovers(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    service = RuntimeService(database)
    service.start()
    service.update_connection("DISCONNECTED")
    assert database.get_runtime_state("service")["status"] == "DEGRADED"
    service.update_connection("CONNECTED")
    assert database.get_runtime_state("service")["status"] == "HEALTHY"
    database.close()


def test_self_monitoring_is_persisted_automatically_from_runtime_state(tmp_path):
    database = Database(tmp_path / "self-monitoring.sqlite3")
    service = RuntimeService(database, heartbeat_seconds=1)
    service.start(["EPIC"])
    service.heartbeat(connection="CONNECTED")
    service.update_market("EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC), connection="CONNECTED", market_status="TRADEABLE", market_status_source="IG_MARKET_DETAILS")
    snapshot = service.evaluate_self_monitoring(force=True)
    assert database.get_runtime_state("self_monitoring")["generated_at"] == snapshot["generated_at"]
    assert snapshot["markets"][0]["monitoring_state"] == "MONITORING"
    assert snapshot["markets"][0]["provider_status"] == "TRADEABLE"
    database.close()
