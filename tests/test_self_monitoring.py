import json
import sqlite3
from datetime import UTC, datetime, timedelta

from ig_ai.self_monitoring import (
    ATTENTION,
    DEGRADED,
    HEALTHY,
    UNKNOWN,
    evaluate_health,
    persist_health,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class HealthDatabase:
    def __init__(self, *, state="MONITORING", tick_age=30, provider="TRADEABLE", connection="CONNECTED"):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            "CREATE TABLE runtime_state (state_key TEXT PRIMARY KEY, state_value_json TEXT, updated_at TEXT);"
            "CREATE TABLE instruments (instrument_id TEXT, epic TEXT, market_name TEXT);"
        )
        heartbeat = (NOW - timedelta(seconds=10)).isoformat()
        self.states = {
            "service": {"status": "HEALTHY", "last_heartbeat_at": heartbeat, "ig_connection": connection}
        }
        self.markets = [{
            "instrument_id": "US", "market_status": provider, "market_status_source": "IG_MARKET_DETAILS",
            "market_status_at": heartbeat, "monitoring_state": state,
            "last_tick": (NOW - timedelta(seconds=tick_age)).isoformat(),
        }]
        self.direction = {"candle_state": "CLOSED", "candle_timestamp": heartbeat}
        self.telemetry = {"last_closed_15m_at": heartbeat, "last_closed_1h_at": heartbeat, "last_decision_at": heartbeat, "last_alert_at": None}
        self.connection.execute("INSERT INTO runtime_state VALUES ('service', ?, ?)", (json.dumps(self.states["service"]), heartbeat))
        self.connection.execute("INSERT INTO instruments VALUES ('US', 'IX.US', 'US Tech 100')")

    def get_runtime_state(self, key):
        return self.states.get(key)

    def save_runtime_state(self, key, value):
        self.states[key] = value

    def list_runtime_schedules(self):
        return [{"instrument_id": "US", "mode": "24_7", "timezone": "UTC"}]

    def list_runtime_market_states(self):
        return list(self.markets)

    def get_direction_status(self, _instrument_id):
        return self.direction

    def latest_market_telemetry(self, _instrument_id):
        return dict(self.telemetry)


def test_healthy_runtime_and_fresh_market_is_healthy():
    snapshot = evaluate_health(HealthDatabase(), now=NOW)
    assert snapshot["overall_status"] == HEALTHY
    assert snapshot["markets"][0]["tick_health"] == HEALTHY
    assert snapshot["markets"][0]["analysis_health"] == HEALTHY


def test_disconnected_ig_is_degraded_but_market_health_is_separate():
    snapshot = evaluate_health(HealthDatabase(connection="DISCONNECTED"), now=NOW)
    assert snapshot["overall_status"] == DEGRADED
    assert snapshot["ig"]["connection_status"] == "DISCONNECTED"


def test_closed_market_with_old_tick_is_normal():
    database = HealthDatabase(state="MARKET_CLOSED", tick_age=7200, provider="CLOSED")
    snapshot = evaluate_health(database, now=NOW)
    assert snapshot["overall_status"] == HEALTHY
    assert snapshot["markets"][0]["tick_health"] == HEALTHY
    assert snapshot["markets"][0]["monitoring_state"] == "MARKET_CLOSED"


def test_provider_open_stale_market_creates_attention():
    snapshot = evaluate_health(HealthDatabase(state="STALE_DATA", tick_age=7200), now=NOW)
    assert snapshot["overall_status"] == ATTENTION
    assert snapshot["markets"][0]["tick_health"] == ATTENTION
    assert snapshot["incidents"][0]["id"] == "market-stale:US"


def test_unknown_provider_and_scheduled_off_are_conservative():
    unknown = evaluate_health(HealthDatabase(state="UNKNOWN", provider=None), now=NOW)
    assert unknown["markets"][0]["tick_health"] == UNKNOWN
    scheduled = HealthDatabase(state="SCHEDULED_OFF", tick_age=7200, provider="UNKNOWN")
    assert evaluate_health(scheduled, now=NOW)["overall_status"] == HEALTHY


def test_forming_one_hour_does_not_create_analysis_failure():
    database = HealthDatabase()
    database.direction = {"candle_state": "FORMING", "candle_timestamp": NOW.isoformat()}
    database.telemetry["last_closed_1h_at"] = None
    snapshot = evaluate_health(database, now=NOW)
    assert snapshot["markets"][0]["analysis_health"] == UNKNOWN
    assert not any(item["domain"] == "ANALYSIS" for item in snapshot["incidents"])


def test_closed_one_hour_with_analysis_is_healthy_and_decision_can_be_null():
    database = HealthDatabase()
    database.telemetry["last_decision_at"] = None
    snapshot = evaluate_health(database, now=NOW)
    assert snapshot["markets"][0]["analysis_health"] == HEALTHY
    assert snapshot["markets"][0]["latest_decision"] is None


def test_database_failure_is_machine_readable():
    database = HealthDatabase()
    class BrokenConnection:
        def execute(self, *_args):
            raise sqlite3.OperationalError("read failed")
    database.connection = BrokenConnection()
    snapshot = evaluate_health(database, now=NOW)
    assert snapshot["database"]["status"] == UNKNOWN
    assert snapshot["overall_status"] == DEGRADED


def test_incident_transitions_are_deduplicated_and_resolved():
    database = HealthDatabase(state="STALE_DATA", tick_age=7200)
    first = persist_health(database, evaluate_health(database, now=NOW))
    second = persist_health(database, evaluate_health(database, now=NOW + timedelta(seconds=30)))
    assert first["incidents"][0]["transition"] == "OPENED"
    assert second["incidents"][0]["transition"] == "ONGOING"
    database.markets[0]["monitoring_state"] = "MONITORING"
    database.markets[0]["last_tick"] = (NOW + timedelta(seconds=31)).isoformat()
    resolved = persist_health(database, evaluate_health(database, now=NOW + timedelta(seconds=31)))
    assert resolved["incidents"][0]["transition"] == "RESOLVED"


def test_health_snapshot_contains_no_secrets():
    snapshot = evaluate_health(HealthDatabase(), now=NOW)
    encoded = json.dumps(snapshot).lower()
    assert "password" not in encoded
    assert "api_key" not in encoded
    assert "security_token" not in encoded
