"""Conservative, read-only production self-monitoring derived from persisted state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .market_identity import canonical_market_label

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
ATTENTION = "ATTENTION"
UNKNOWN = "UNKNOWN"
MARKET_NORMAL_STATES = {"MONITORING", "MARKET_CLOSED", "SCHEDULED_OFF"}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _age(value: str | None, now: datetime) -> float | None:
    parsed = _parse(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def _status_from_incidents(incidents: list[dict[str, Any]]) -> str:
    if any(item["severity"] == DEGRADED for item in incidents):
        return DEGRADED
    if incidents:
        return ATTENTION
    return HEALTHY


def _incident(
    incident_id: str,
    severity: str,
    reason: str,
    domain: str,
    market: str | None = None,
) -> dict[str, Any]:
    item = {
        "id": incident_id,
        "severity": severity,
        "domain": domain,
        "reason": reason,
        "status": "ACTIVE",
    }
    if market:
        item["market"] = market
    return item


def evaluate_health(
    database,
    *,
    now: datetime | None = None,
    stale_seconds: float = 900.0,
    heartbeat_seconds: float = 30.0,
) -> dict[str, Any]:
    """Evaluate persisted runtime evidence without contacting IG or mutating analytics."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    incidents: list[dict[str, Any]] = []
    service: dict[str, Any] = {}
    markets: list[dict[str, Any]] = []
    schedules: dict[str, dict[str, Any]] = {}
    database_health: dict[str, Any]
    try:
        database.connection.execute("SELECT 1").fetchone()
        service_row = database.connection.execute(
            "SELECT state_value_json, updated_at FROM runtime_state WHERE state_key='service'"
        ).fetchone()
        service = database.get_runtime_state("service") or {}
        persistence_timestamp = service_row[1] if service_row else None
        schedule_rows = database.list_runtime_schedules()
        schedules = {row["instrument_id"]: row for row in schedule_rows}
        names = {
            row[0]: canonical_market_label(row[2]) or canonical_market_label(row[1]) or row[2]
            for row in database.connection.execute("SELECT instrument_id, epic, market_name FROM instruments")
        }
        runtime_rows = {
            row["instrument_id"]: row for row in database.list_runtime_market_states()
        }
        database_health = {
            "status": HEALTHY,
            "persistence_timestamp": persistence_timestamp,
            "evidence": "runtime_state readable",
        }
        for instrument_id, runtime in runtime_rows.items():
            market = names.get(instrument_id, "UNKNOWN")
            schedule = schedules.get(instrument_id, {"mode": "UNKNOWN", "timezone": "UNKNOWN"})
            state = runtime.get("monitoring_state", UNKNOWN)
            tick_age = _age(runtime.get("last_tick"), now)
            if state == "STALE_DATA":
                tick_health = ATTENTION
                incidents.append(_incident(
                    f"market-stale:{instrument_id}", ATTENTION,
                    "provider market is active but the latest tick is stale",
                    "MARKET_DATA", market,
                ))
            elif state in {"MARKET_CLOSED", "SCHEDULED_OFF"}:
                tick_health = HEALTHY
            elif state == "MONITORING" and tick_age is not None and tick_age <= stale_seconds:
                tick_health = HEALTHY
            elif state == UNKNOWN:
                tick_health = UNKNOWN
            else:
                tick_health = UNKNOWN

            try:
                direction_row = database.connection.execute(
                    "SELECT snapshot_json FROM direction_snapshots "
                    "WHERE instrument_id=? AND timeframe='1H' AND is_closed=1 "
                    "ORDER BY candle_start DESC LIMIT 1",
                    (instrument_id,),
                ).fetchone()
            except Exception:
                direction_row = None
            direction = (
                json.loads(direction_row[0])
                if direction_row
                else database.get_direction_status(instrument_id) or database.get_direction_status(market)
            )
            latest_analysis = None
            if direction and direction.get("candle_state") == "CLOSED":
                latest_analysis = direction.get("candle_timestamp")
            telemetry = database.latest_market_telemetry(instrument_id)
            latest_closed_1h = telemetry.get("last_closed_1h_at")
            if latest_closed_1h and latest_analysis:
                analysis_status = HEALTHY
            elif latest_closed_1h:
                analysis_status = UNKNOWN
                if (_age(latest_closed_1h, now) or 0) > max(heartbeat_seconds * 3, 300):
                    analysis_status = ATTENTION
                    incidents.append(_incident(
                        f"analysis-behind:{instrument_id}", ATTENTION,
                        "eligible closed 1H evidence has no persisted analysis yet",
                        "ANALYSIS", market,
                    ))
            else:
                analysis_status = UNKNOWN

            market_item = {
                "market": market,
                "epic": next((row[1] for row in database.connection.execute(
                    "SELECT instrument_id, epic FROM instruments WHERE instrument_id=?", (instrument_id,)
                )), None),
                "provider_status": runtime.get("market_status") or UNKNOWN,
                "market_status_source": runtime.get("market_status_source") or UNKNOWN,
                "market_status_at": runtime.get("market_status_at"),
                "monitoring_state": state,
                "schedule_mode": schedule.get("mode", UNKNOWN),
                "tick_health": tick_health,
                "latest_tick": runtime.get("last_tick"),
                "tick_age_seconds": tick_age,
                "latest_closed_15m": telemetry.get("last_closed_15m_at"),
                "latest_closed_1h": latest_closed_1h,
                "latest_analysis": latest_analysis,
                "analysis_health": analysis_status,
                "latest_decision": telemetry.get("last_decision_at"),
                "latest_alert": telemetry.get("last_alert_at"),
            }
            markets.append(market_item)

        heartbeat = service.get("last_heartbeat_at")
        heartbeat_age = _age(heartbeat, now)
        service_status = HEALTHY if heartbeat_age is not None and heartbeat_age <= heartbeat_seconds * 3 else DEGRADED
        if heartbeat_age is None:
            service_status = UNKNOWN
            incidents.append(_incident("service-heartbeat", DEGRADED, "runtime heartbeat is unavailable", "SERVICE"))
        elif heartbeat_age > heartbeat_seconds * 3:
            incidents.append(_incident("service-heartbeat", DEGRADED, "runtime heartbeat is not advancing", "SERVICE"))
        connection = service.get("ig_connection") or UNKNOWN
        ig_status = HEALTHY if connection == "CONNECTED" and service_status == HEALTHY else (
            DEGRADED if connection in {"DISCONNECTED", "RECONNECTING"} else UNKNOWN
        )
        if ig_status == DEGRADED:
            incidents.append(_incident("ig-connection", DEGRADED, f"IG connection is {connection}", "IG_CONNECTION"))
        alert_times = [item.get("latest_alert") for item in markets if item.get("latest_alert")]
        alerts = {
            "status": HEALTHY if alert_times else UNKNOWN,
            "latest_alert": max(alert_times) if alert_times else None,
            "evidence": "persisted alert timestamps" if alert_times else "no alert observed; no synthetic alert generated",
        }
    except Exception:  # health must fail closed without exposing exception details
        database_health = {"status": UNKNOWN, "persistence_timestamp": None, "evidence": "database read unavailable"}
        service = {}
        markets = []
        schedules = {}
        alerts = {"status": UNKNOWN, "latest_alert": None, "evidence": "unavailable"}
        incidents.append(_incident("database-read", DEGRADED, "database read/persistence evidence is unavailable", "DATABASE"))

    return {
        "generated_at": now.isoformat(),
        "overall_status": _status_from_incidents(incidents),
        "service": {
            "status": service.get("status", UNKNOWN),
            "health": HEALTHY if service.get("status") == "HEALTHY" else UNKNOWN,
            "heartbeat": service.get("last_heartbeat_at"),
            "heartbeat_age_seconds": _age(service.get("last_heartbeat_at"), now),
        },
        "ig": {
            "connection_status": service.get("ig_connection", UNKNOWN),
            "heartbeat": service.get("last_heartbeat_at"),
        },
        "database": database_health,
        "alerts": alerts,
        "markets": markets,
        "incidents": incidents,
        "schedules": list(schedules.values()),
    }


def persist_health(database, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist a snapshot and transition incidents without emitting duplicates."""
    previous = database.get_runtime_state("self_monitoring") or {}
    previous_by_id = {item.get("id"): item for item in previous.get("incidents", [])}
    active = []
    for item in snapshot["incidents"]:
        old = previous_by_id.get(item["id"])
        item = dict(item)
        item["first_seen_at"] = old.get("first_seen_at", snapshot["generated_at"]) if old else snapshot["generated_at"]
        item["last_seen_at"] = snapshot["generated_at"]
        item["transition"] = "ONGOING" if old else "OPENED"
        active.append(item)
    for old in previous.get("incidents", []):
        if old.get("id") not in {item["id"] for item in active}:
            resolved = dict(old)
            resolved.update({"status": "RESOLVED", "transition": "RESOLVED", "resolved_at": snapshot["generated_at"]})
            active.append(resolved)
    snapshot = {**snapshot, "incidents": active}
    database.save_runtime_state("self_monitoring", snapshot)
    return snapshot
