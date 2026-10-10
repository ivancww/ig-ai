"""Phase 9A service lifecycle, schedules, and Web-App-ready read models."""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as clock_time
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database
from .health_contract import HealthContract

log = logging.getLogger(__name__)

SERVICE_STATES = {"STARTING", "HEALTHY", "DEGRADED", "STOPPING"}
CONNECTION_STATES = {"CONNECTED", "RECONNECTING", "DISCONNECTED", "UNKNOWN"}
MARKET_STATES = {"MONITORING", "MARKET_CLOSED", "SCHEDULED_OFF", "STALE_DATA", "UNKNOWN"}
SCHEDULE_MODES = {"24_7", "MARKET_HOURS", "CUSTOM"}
PROVIDER_CLOSED_STATUSES = {"CLOSED", "CLOSED_NO_TRADING", "EDITS_ONLY", "OFFLINE"}
PROVIDER_OPEN_STATUSES = {"OPEN", "TRADEABLE"}


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
        self.database = database
        self.health_contract = HealthContract(
            heartbeat_seconds=heartbeat_seconds,
            stale_data_seconds=stale_seconds,
        )
        self.heartbeat_seconds = self.health_contract.heartbeat_seconds
        self.stale_seconds = self.health_contract.stale_data_seconds
        self.started_at: datetime | None = None
        self.runtime_generation: str | None = None
        self.last_heartbeat: datetime | None = None
        self.stop_requested = threading.Event()
        self._last_persisted_heartbeat: datetime | None = None
        self._last_self_monitoring: float | None = None

    def start(self, instruments: list[str] | tuple[str, ...] = ()) -> None:
        self.started_at = datetime.now(UTC)
        self.runtime_generation = uuid4().hex
        self.stop_requested.clear()
        self.database.save_runtime_state("service", {
            "status": "STARTING",
            "service_started_at": self.started_at.isoformat(),
            "runtime_generation": self.runtime_generation,
            "last_heartbeat_at": None,
            "last_market_data_at": None,
            "ig_connection": "UNKNOWN",
            "startup_failure": None,
        })
        self.database.save_runtime_state("self_monitoring", {
            "runtime_generation": self.runtime_generation,
            "generated_at": self.started_at.isoformat(),
            "overall_status": "UNKNOWN",
            "generation_status": "STARTING",
            "health_contract": self.health_contract.as_dict(),
            "incidents": [],
        })
        self.register_instruments(instruments)
        log.info("service startup requested generation=%s", self.runtime_generation)

    def register_instruments(self, instruments: list[str] | tuple[str, ...] = ()) -> None:
        """Register discovered instruments without changing the process generation."""
        for instrument_id in instruments:
            existing = next((state for state in self.database.list_runtime_market_states() if state.get("instrument_id") == instrument_id), None)
            self.database.save_runtime_market_state(instrument_id, existing or {"instrument_id": instrument_id, "monitoring_state": "UNKNOWN"})

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
        service.update({"status": "HEALTHY" if connection == "CONNECTED" else "DEGRADED", "service_started_at": (self.started_at or now).isoformat(), "runtime_generation": self.runtime_generation, "last_heartbeat_at": now.isoformat(), "ig_connection": connection, "last_market_data_at": max(market_data.values(), default=None).isoformat() if market_data else service.get("last_market_data_at")})
        self.database.save_runtime_state("service", service)
        self._last_persisted_heartbeat = now

    def update_connection(self, state: str) -> None:
        if state not in CONNECTION_STATES:
            state = "UNKNOWN"
        current = self.database.get_runtime_state("service") or {}
        current["ig_connection"] = state
        current["status"] = "HEALTHY" if state == "CONNECTED" else "DEGRADED"
        current["runtime_generation"] = self.runtime_generation
        self.database.save_runtime_state("service", current)
        log.info("IG connection state changed: %s", state)

    def evaluate_self_monitoring(self, *, force: bool = False) -> dict:
        """Persist a bounded health snapshot from the current runtime evidence."""
        now = time.monotonic()
        if not force and self._last_self_monitoring is not None and now - self._last_self_monitoring < self.heartbeat_seconds:
            return self.database.get_runtime_state("self_monitoring") or {}
        from .self_monitoring import evaluate_health, persist_health

        snapshot = evaluate_health(
            self.database,
            stale_seconds=self.stale_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            expected_generation=self.runtime_generation,
        )
        self._last_self_monitoring = now
        return persist_health(self.database, snapshot)

    def update_market(
        self,
        instrument_id: str,
        *,
        schedule: Schedule,
        last_data: datetime | None = None,
        connection: str = "UNKNOWN",
        market_status: str | None = None,
        market_status_source: str | None = None,
        market_status_at: datetime | None = None,
    ) -> dict:
        now = datetime.now(UTC)
        active = schedule.is_active(now)
        normalized_status = (market_status or "").upper()
        if active is False:
            state = "SCHEDULED_OFF"
        elif normalized_status in PROVIDER_CLOSED_STATUSES:
            state = "MARKET_CLOSED"
        elif active is None:
            state = "UNKNOWN"
        elif normalized_status not in PROVIDER_OPEN_STATUSES:
            # A recent tick is evidence of recent data, not authoritative
            # proof of current IG dealing availability. 24_7 is a monitor
            # schedule and never supplies market-session hours.
            state = "UNKNOWN"
        elif last_data is None:
            state = "UNKNOWN"
        elif (now - last_data).total_seconds() <= self.stale_seconds:
            state = "MONITORING"
        else:
            state = "STALE_DATA"
        result = {
            "instrument_id": instrument_id,
            "monitoring_state": state,
            "ig_connection": connection,
            "last_tick": last_data.isoformat() if last_data else None,
            "last_heartbeat_at": now.isoformat(),
            "market_status": market_status,
            "market_status_source": market_status_source,
            "market_status_at": market_status_at.isoformat() if market_status_at else None,
            **self.database.latest_market_telemetry(instrument_id),
        }
        self.database.save_runtime_market_state(instrument_id, result)
        return result

    def stop(self) -> None:
        now = datetime.now(UTC)
        current = self.database.get_runtime_state("service") or {}
        current.update({"status": "STOPPING", "runtime_generation": self.runtime_generation, "last_heartbeat_at": now.isoformat()})
        self.database.save_runtime_state("service", current)
        log.info("service stopping")

    def record_startup_failure(self, error: Exception) -> None:
        """Persist a safe startup failure without masking the original error."""
        from .exceptions import IGHTTPError

        if isinstance(error, IGHTTPError):
            category = f"IG_HTTP_{error.status}" if error.status else "IG_NETWORK_FAILURE"
            diagnostic = error.safe_diagnostic()
        else:
            category = type(error).__name__.upper()
            diagnostic = f"startup failure category={category}"
        current = self.database.get_runtime_state("service") or {}
        current.update({
            "status": "DEGRADED",
            "runtime_generation": self.runtime_generation,
            "startup_failure": {"category": category, "diagnostic": diagnostic},
        })
        self.database.save_runtime_state("service", current)
        self.evaluate_self_monitoring(force=True)

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
    import time
    from datetime import datetime

    from .discovery import discover_market_groups, refresh_market_status, select_stream_instruments
    from .models import Instrument
    from .rest import IGRestClient
    from .runtime import PersistedStream
    from .streaming import (
        IGStreamService,
        OfficialLightstreamerTransport,
        Subscription,
        lightstreamer_password,
    )

    database = Database(settings.database_path)
    service = RuntimeService(database, heartbeat_seconds=settings.service_heartbeat_seconds, stale_seconds=settings.stale_data_seconds)
    service.start()
    try:
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
        instruments = {candidate.epic: Instrument(candidate.epic, candidate.epic, candidate.market_name, candidate.instrument_type, candidate.market_status, candidate.metadata) for candidate in selected}
        service.register_instruments(list(instruments))
        for instrument in instruments.values():
            database.save_instrument_model(instrument)
            database.save_runtime_schedule(instrument.instrument_id, Schedule(settings.monitoring_mode, settings.custom_windows, settings.runtime_timezone).as_dict())
        schedules = {instrument_id: Schedule(settings.monitoring_mode, settings.custom_windows, settings.runtime_timezone) for instrument_id in instruments}
        status_refresh_seconds = settings.market_status_refresh_seconds
        status_cache = {
            instrument_id: {
                "status": instrument.market_status,
                "source": "DISCOVERY",
                "refreshed_at": datetime.now(UTC),
            }
            for instrument_id, instrument in instruments.items()
        }
        last_status_refresh = time.monotonic()

        def refresh_market_statuses() -> None:
            nonlocal last_status_refresh
            now_monotonic = time.monotonic()
            if now_monotonic - last_status_refresh < status_refresh_seconds:
                return
            last_status_refresh = now_monotonic
            refreshed_at = datetime.now(UTC)
            for instrument_id, instrument in instruments.items():
                try:
                    status = refresh_market_status(client, instrument.epic)
                    status_cache[instrument_id] = {
                        "status": status,
                        "source": "IG_MARKET_DETAILS" if status else "IG_MARKET_DETAILS_UNAVAILABLE",
                        "refreshed_at": refreshed_at,
                    }
                except Exception as exc:  # provider status is advisory; stream remains read-only
                    status_cache[instrument_id] = {
                        "status": None,
                        "source": f"IG_MARKET_DETAILS_ERROR:{type(exc).__name__}",
                        "refreshed_at": refreshed_at,
                    }
                    log.warning("market status refresh unavailable for %s: %s", instrument.epic, type(exc).__name__)

        stream = IGStreamService(session.lightstreamer_endpoint, session.account_id, lightstreamer_password(session.cst, session.security_token), OfficialLightstreamerTransport(), reconnect_seconds=settings.stream_reconnect_seconds)
        for instrument in instruments.values():
            stream.add_subscription(Subscription(instrument.instrument_id, f"PRICE:{session.account_id}:{instrument.epic}"))

        def heartbeat(last_data: dict[str, datetime]) -> None:
            refresh_market_statuses()
            connection = stream.stats.connection_state if stream.stats.connection_state in CONNECTION_STATES else "UNKNOWN"
            service.heartbeat(connection=connection, market_data=last_data)
            for instrument_id, schedule in schedules.items():
                status = status_cache[instrument_id]
                service.update_market(
                    instrument_id,
                    schedule=schedule,
                    last_data=last_data.get(instrument_id),
                    connection=connection,
                    market_status=status["status"],
                    market_status_source=status["source"],
                    market_status_at=status["refreshed_at"],
                )
            service.evaluate_self_monitoring()

        sink = PersistedStream(database, instruments, settings.market_timezone, session_started_at=datetime.now(UTC), update_filter=lambda update: schedules.get(str(update.get("instrument_id")), Schedule("MARKET_HOURS")).is_active() is True, heartbeat_callback=heartbeat, stop_requested=service.stop_requested)
        original_term_handler = signal.getsignal(signal.SIGTERM)
        original_int_handler = signal.getsignal(signal.SIGINT)
        def shutdown(_signum, _frame):
            service.request_shutdown()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        service.update_connection("RECONNECTING")
        sink.run_for(stream, 10 * 365 * 24 * 60 * 60)
        service.update_connection(stream.stats.connection_state if stream.stats.connection_state in CONNECTION_STATES else "UNKNOWN")
        return 0
    except Exception as error:
        service.record_startup_failure(error)
        raise
    finally:
        service.stop()
        database.close()
        if "original_term_handler" in locals():
            signal.signal(signal.SIGTERM, original_term_handler)
            signal.signal(signal.SIGINT, original_int_handler)
