"""Phase 9A service lifecycle, schedules, and Web-App-ready read models."""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database

log = logging.getLogger(__name__)

SERVICE_STATES = {"STARTING", "HEALTHY", "DEGRADED", "STOPPING"}
CONNECTION_STATES = {"CONNECTED", "RECONNECTING", "DISCONNECTED", "UNKNOWN"}
MARKET_STATES = {"MONITORING", "MARKET_CLOSED", "SCHEDULED_OFF", "STALE_DATA", "UNKNOWN"}
SCHEDULE_MODES = {"24_7", "MARKET_HOURS", "CUSTOM"}


def parse_custom_windows(raw: str) -> tuple[str, ...]:
    """Parse the external schedule contract: semicolon-separated windows."""
    parts = raw.split(";")
    windows = tuple(part.strip() for part in parts)
    if not windows or any(not window for window in windows):
        raise ValueError("IGAI_CUSTOM_WINDOWS must contain one or more ';'-separated windows")
    for window in windows:
        _parse_window(window)
    return windows


@dataclass(frozen=True)
class Schedule:
    mode: str = "24_7"
    windows: tuple[str, ...] = ()
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if self.mode not in SCHEDULE_MODES:
            raise ValueError(f"unsupported monitoring mode: {self.mode}")
        if self.mode == "CUSTOM" and not self.windows:
            raise ValueError("CUSTOM schedules require at least one window")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown schedule timezone: {self.timezone}") from exc
        for window in self.windows:
            _parse_window(window)

    def is_active(self, now: datetime | None = None) -> bool | None:
        """Return None when MARKET_HOURS has no verified rules."""
        if self.mode == "24_7":
            return True
        if self.mode == "MARKET_HOURS":
            return None
        local = (now or datetime.now(UTC)).astimezone(ZoneInfo(self.timezone))
        return any(_window_active(window, local) for window in self.windows)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "windows": list(self.windows), "timezone": self.timezone}


def _parse_window(window: str) -> tuple[set[int], clock_time, clock_time]:
    try:
        days, hours = window.split(" ", 1)
        start, end = hours.split("-", 1)
        weekdays = {int(day) for day in days.split(",")}
        if not weekdays or not weekdays <= set(range(1, 8)):
            raise ValueError
        return weekdays, clock_time.fromisoformat(start), clock_time.fromisoformat(end)
    except (ValueError, TypeError):
        raise ValueError(f"invalid custom schedule window: {window!r}") from None


def _window_active(window: str, local: datetime) -> bool:
    weekdays, start, end = _parse_window(window)
    current = local.timetz().replace(tzinfo=None)
    weekday = local.isoweekday()
    if start <= end:
        return weekday in weekdays and start <= current < end
    # The after-midnight portion belongs to the session that started on the
    # prior configured weekday (Friday 20:00-05:00 includes Saturday 01:00).
    previous_weekday = 7 if weekday == 1 else weekday - 1
    return (weekday in weekdays and current >= start) or (
        previous_weekday in weekdays and current < end
    )


class RuntimeService:
    """Owns service state; the existing PersistedStream owns market analysis."""

    def __init__(self, database: Database, *, heartbeat_seconds: float = 30.0, stale_seconds: float = 900.0):
        if heartbeat_seconds <= 0 or stale_seconds <= 0:
            raise ValueError("heartbeat and stale intervals must be positive")
        self.database = database
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_seconds = stale_seconds
        self.started_at: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.stop_requested = threading.Event()
        self._last_persisted_heartbeat: datetime | None = None

    def start(self, instruments: list[str] | tuple[str, ...] = ()) -> None:
        self.started_at = datetime.now(UTC)
        self.stop_requested.clear()
        self.database.save_runtime_state("service", {"status": "STARTING", "service_started_at": self.started_at.isoformat()})
        for instrument_id in instruments:
            existing = next((state for state in self.database.list_runtime_market_states() if state.get("instrument_id") == instrument_id), None)
            self.database.save_runtime_market_state(instrument_id, existing or {"instrument_id": instrument_id, "monitoring_state": "UNKNOWN"})
        log.info("service startup requested")

    def request_shutdown(self, *_args) -> None:
        self.stop_requested.set()
        log.info("graceful shutdown requested")

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)

    def heartbeat(self, *, connection: str = "UNKNOWN", market_data: dict[str, datetime] | None = None) -> None:
        now = datetime.now(UTC)
        self.last_heartbeat = now
        if self._last_persisted_heartbeat and (now - self._last_persisted_heartbeat).total_seconds() < self.heartbeat_seconds:
            return
        market_data = market_data or {}
        service = self.database.get_runtime_state("service") or {}
        service.update({"status": "HEALTHY" if connection == "CONNECTED" else "DEGRADED", "service_started_at": (self.started_at or now).isoformat(), "last_heartbeat_at": now.isoformat(), "ig_connection": connection, "last_market_data_at": max(market_data.values(), default=None).isoformat() if market_data else service.get("last_market_data_at")})
        self.database.save_runtime_state("service", service)
        self._last_persisted_heartbeat = now

    def update_connection(self, state: str) -> None:
        if state not in CONNECTION_STATES:
            state = "UNKNOWN"
        current = self.database.get_runtime_state("service") or {}
        current["ig_connection"] = state
        current["status"] = "HEALTHY" if state == "CONNECTED" else "DEGRADED"
        self.database.save_runtime_state("service", current)
        log.info("IG connection state changed: %s", state)

    def update_market(self, instrument_id: str, *, schedule: Schedule, last_data: datetime | None = None, connection: str = "UNKNOWN", market_status: str | None = None) -> dict:
        now = datetime.now(UTC)
        active = schedule.is_active(now)
        normalized_status = (market_status or "").upper()
        if normalized_status in {"CLOSED", "CLOSED_NO_TRADING"}:
            state = "MARKET_CLOSED"
        elif active is False:
            state = "SCHEDULED_OFF"
        elif active is None:
            state = "UNKNOWN"
        elif last_data is None:
            state = "UNKNOWN"
        elif (now - last_data).total_seconds() > self.stale_seconds:
            state = "STALE_DATA"
        else:
            state = "MONITORING"
        result = {
            "instrument_id": instrument_id,
            "monitoring_state": state,
            "ig_connection": connection,
            "last_tick": last_data.isoformat() if last_data else None,
            "last_heartbeat_at": now.isoformat(),
            **self.database.latest_market_telemetry(instrument_id),
        }
        self.database.save_runtime_market_state(instrument_id, result)
        return result

    def stop(self) -> None:
        now = datetime.now(UTC)
        current = self.database.get_runtime_state("service") or {}
        current.update({"status": "STOPPING", "last_heartbeat_at": now.isoformat()})
        self.database.save_runtime_state("service", current)
        log.info("service stopping")

    def status(self) -> dict:
        service = self.database.get_runtime_state("service") or {}
        return {"service": service, "markets": self.database.list_runtime_market_states(), "schedules": self.database.list_runtime_schedules()}


def format_service_status(read_model: dict) -> str:
    service = read_model.get("service", {})
    schedules = {item.get("instrument_id"): item for item in read_model.get("schedules", [])}
    started = service.get("service_started_at")
    uptime = "NOT AVAILABLE"
    if started:
        try:
            uptime = str(max(0, int((datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()))) + "s"
        except ValueError:
            pass
    lines = [f"Service: {service.get('status', 'UNKNOWN')}", f"Uptime: {uptime}", f"IG Connection: {service.get('ig_connection', 'UNKNOWN')}", f"Last Heartbeat: {service.get('last_heartbeat_at', 'NOT AVAILABLE')}", f"Last Market Data: {service.get('last_market_data_at', 'NOT AVAILABLE')}"]
    for market in read_model.get("markets", []):
        schedule = schedules.get(market.get("instrument_id"), {})
        schedule_text = "NOT AVAILABLE"
        if schedule:
            schedule_text = f"{schedule.get('mode', 'UNKNOWN')} {schedule.get('windows', [])} ({schedule.get('timezone', 'UNKNOWN')})"
        lines.extend(["", f"{market.get('instrument_id', 'UNKNOWN')}:", f"Schedule: {schedule_text}", f"Monitoring State: {market.get('monitoring_state', 'UNKNOWN')}", f"Last Tick: {market.get('last_tick') or 'NOT AVAILABLE'}", f"Last 15M: {market.get('last_closed_15m_at') or 'NOT AVAILABLE'}", f"Last 1H: {market.get('last_closed_1h_at') or 'NOT AVAILABLE'}", f"Last Decision: {market.get('last_decision_at') or 'NOT AVAILABLE'}", f"Last Alert: {market.get('last_alert_at') or 'NOT AVAILABLE'}"])
    return "\n".join(lines)


def run_live_service(settings, markets: str = "US Tech 100,Japan 225,Hong Kong HS50") -> int:
    """Start the existing discovery/Lightstreamer/PersistedStream stack indefinitely."""
    from datetime import datetime

    from .discovery import discover_market_groups, select_stream_instruments
    from .models import Instrument
    from .rest import IGRestClient
    from .runtime import PersistedStream
    from .streaming import (
        IGStreamService,
        OfficialLightstreamerTransport,
        Subscription,
        lightstreamer_password,
    )

    client = IGRestClient(settings)
    groups = discover_market_groups(client)
    requested = {name.strip() for name in markets.split(",") if name.strip()}
    selected = [candidate for candidate in select_stream_instruments(groups) if candidate.requested_market in requested]
    missing = sorted(requested - {candidate.requested_market for candidate in selected})
    if missing:
        raise RuntimeError("No provider-verified weekday cash instrument for: " + ", ".join(missing))
    session = client.ensure_session()
    if not session.lightstreamer_endpoint or not session.account_id:
        raise RuntimeError("IG authentication did not provide a Lightstreamer endpoint/account")
    database = Database(settings.database_path)
    service = RuntimeService(database, heartbeat_seconds=settings.service_heartbeat_seconds, stale_seconds=settings.stale_data_seconds)
    instruments = {candidate.epic: Instrument(candidate.epic, candidate.epic, candidate.market_name, candidate.instrument_type, candidate.market_status, candidate.metadata) for candidate in selected}
    for instrument in instruments.values():
        database.save_instrument_model(instrument)
        database.save_runtime_schedule(instrument.instrument_id, Schedule(settings.monitoring_mode, settings.custom_windows, settings.runtime_timezone).as_dict())
    schedules = {instrument_id: Schedule(settings.monitoring_mode, settings.custom_windows, settings.runtime_timezone) for instrument_id in instruments}
    stream = IGStreamService(session.lightstreamer_endpoint, session.account_id, lightstreamer_password(session.cst, session.security_token), OfficialLightstreamerTransport(), reconnect_seconds=settings.stream_reconnect_seconds)
    for instrument in instruments.values():
        stream.add_subscription(Subscription(instrument.instrument_id, f"PRICE:{session.account_id}:{instrument.epic}"))
    service.start(list(instruments))
    def heartbeat(last_data: dict[str, datetime]) -> None:
        connection = stream.stats.connection_state if stream.stats.connection_state in CONNECTION_STATES else "UNKNOWN"
        service.heartbeat(connection=connection, market_data=last_data)
        for instrument_id, schedule in schedules.items():
            service.update_market(instrument_id, schedule=schedule, last_data=last_data.get(instrument_id), connection=connection, market_status=instruments[instrument_id].market_status)

    sink = PersistedStream(database, instruments, settings.market_timezone, session_started_at=datetime.now(UTC), update_filter=lambda update: schedules.get(str(update.get("instrument_id")), Schedule("MARKET_HOURS")).is_active() is True, heartbeat_callback=heartbeat)
    original_term_handler = signal.getsignal(signal.SIGTERM)
    original_int_handler = signal.getsignal(signal.SIGINT)
    def shutdown(_signum, _frame):
        service.request_shutdown()
        stream.stop(reason="shutdown")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        service.update_connection("RECONNECTING")
        sink.run_for(stream, 10 * 365 * 24 * 60 * 60)
        service.update_connection(stream.stats.connection_state if stream.stats.connection_state in CONNECTION_STATES else "UNKNOWN")
        return 0
    finally:
        service.stop()
        database.close()
        signal.signal(signal.SIGTERM, original_term_handler)
        signal.signal(signal.SIGINT, original_int_handler)
