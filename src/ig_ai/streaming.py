from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)
REQUIRED_FIELDS = ("BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG")


def lightstreamer_password(cst: str, security_token: str) -> str:
    return f"CST-{cst}|XST-{security_token}"


def configure_lightstreamer_connection(client: Any, username: str, password: str) -> None:
    """Configure credentials through the official SDK 2.2.3 API boundary."""
    connection_details = client.connectionDetails
    connection_details.setUser(username)
    connection_details.setPassword(password)


def _safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


class LightstreamerError(ConnectionError):
    def __init__(self, message: str, *, endpoint: str, phase: str, retryable: bool = True):
        super().__init__(message)
        self.endpoint = _safe_endpoint(endpoint)
        self.phase = phase
        self.retryable = retryable

    def safe_diagnostic(self) -> str:
        return f"Lightstreamer diagnostic: phase={self.phase} endpoint={self.endpoint}"


class StreamTransport(Protocol):
    def connect(self, endpoint: str, username: str, password: str) -> None: ...
    def send(self, command: str, params: dict[str, str]) -> None: ...
    def receive(self) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Subscription:
    instrument_id: str
    item: str
    fields: tuple[str, ...] = REQUIRED_FIELDS
    data_adapter: str = "Pricing"


@dataclass
class StreamDiagnostics:
    """Safe SDK lifecycle evidence; no authenticated values are retained."""

    connection_state: str = "DISCONNECTED"
    connection_verified: bool = False
    reconnect_count: int = 0
    subscriptions_accepted: set[str] = field(default_factory=set)
    first_updates_received: set[str] = field(default_factory=set)
    subscription_states: dict[str, str] = field(default_factory=dict)
    sdk_statuses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    intentional_shutdown: bool = False
    duration_expired: bool = False
    unexpected_disconnect: bool = False
    sdk_client_created: bool = False
    connect_invoked: bool = False
    connection_wait_seconds: float = 0.0
    server_error_code: int | None = None
    server_error_message: str | None = None
    client_lifecycle_state: str = "NOT_CREATED"
    runtime_failures: list[dict[str, str]] = field(default_factory=list)


@dataclass
class StreamStats:
    connection_state: str = "DISCONNECTED"
    reconnect_count: int = 0
    updates_received: dict[str, int] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    diagnostics: StreamDiagnostics = field(default_factory=StreamDiagnostics)

    def live_validation_passed(self, instrument_ids: list[str] | tuple[str, ...]) -> bool:
        """Require independent subscription and real update evidence per market."""
        return bool(
            self.diagnostics.connection_verified
            and instrument_ids
            and all(
                self.updates_received.get(instrument_id, 0) > 0
                and str(index) in self.diagnostics.subscriptions_accepted
                for index, instrument_id in enumerate(instrument_ids, 1)
            )
        )


class OfficialLightstreamerTransport:
    """Official Lightstreamer Python SDK boundary for IG streaming."""

    def __init__(self, client_factory: Callable[..., Any] | None = None, *, connection_timeout: float = 15.0):
        self._client_factory = client_factory
        self._connection_timeout = connection_timeout
        self._client = None
        self._connection_event = threading.Event()
        self._sensitive_values: tuple[str, ...] = ()
        self._subscriptions: dict[str, Any] = {}
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._subscription_specs: dict[str, dict[str, str]] = {}
        self.diagnostics = StreamDiagnostics()

    def _sdk(self) -> tuple[Any, Any, Any]:
        if self._client_factory is not None:
            try:
                from lightstreamer.client import Subscription as sdk_subscription
                from lightstreamer.client import SubscriptionListener
            except ImportError:
                return self._client_factory, None, object
            return self._client_factory, sdk_subscription, SubscriptionListener
        try:
            from lightstreamer.client import LightstreamerClient, SubscriptionListener
            from lightstreamer.client import Subscription as sdk_subscription
        except ImportError as exc:
            raise RuntimeError("Install ig-ai[streaming] to use the Lightstreamer SDK") from exc
        return LightstreamerClient, sdk_subscription, SubscriptionListener

    def _safe_server_error(self, message: object) -> str:
        safe = str(message)
        for value in self._sensitive_values:
            if value:
                safe = safe.replace(value, "[REDACTED]")
        safe = re.sub(r"(?i)(?:CST|XST)-[^\s|,;]+", "[REDACTED]", safe)
        return safe[:500]

    def connect(self, endpoint: str, username: str, password: str) -> None:
        client_factory, _, _ = self._sdk()
        started = time.monotonic()
        self._connection_event.clear()
        self.diagnostics.connection_state = "DISCONNECTED"
        self.diagnostics.connection_verified = False
        self.diagnostics.connect_invoked = False
        self.diagnostics.connection_wait_seconds = 0.0
        self.diagnostics.server_error_code = None
        self.diagnostics.server_error_message = None
        self.diagnostics.client_lifecycle_state = "NOT_CREATED"
        self._sensitive_values = (username, password)
        try:
            # The endpoint is the exact lightstreamerEndpoint returned by IG.
            # The SDK accepts the server address and adapter set separately; it
            # owns the /lightstreamer transport path and protocol selection.
            self._client = client_factory(endpoint, "DEFAULT")
            self.diagnostics.sdk_client_created = True
            self.diagnostics.client_lifecycle_state = "CREATED"
            configure_lightstreamer_connection(self._client, username, password)
            self.diagnostics.client_lifecycle_state = "CONFIGURED"
            self._client.addListener(_ClientListener(self))
            self.diagnostics.client_lifecycle_state = "LISTENER_ATTACHED"
            self.diagnostics.connect_invoked = True
            self.diagnostics.client_lifecycle_state = "CONNECT_REQUESTED"
            self._client.connect()
        except Exception as exc:
            self.diagnostics.connection_wait_seconds = time.monotonic() - started
            self.diagnostics.client_lifecycle_state = "FAILED"
            raise LightstreamerError(
                "Lightstreamer SDK connection failed", endpoint=endpoint, phase="connect"
            ) from exc

        connected = self._connection_event.wait(self._connection_timeout)
        self.diagnostics.connection_wait_seconds = time.monotonic() - started
        if not connected or not self.diagnostics.connection_verified:
            self.diagnostics.client_lifecycle_state = "FAILED"
            detail = self.diagnostics.server_error_message or "connection status was not CONNECTED"
            raise LightstreamerError(
                f"Lightstreamer SDK connection not established: {detail}",
                endpoint=endpoint,
                phase="connect",
            )

    def send(self, command: str, params: dict[str, str]) -> None:
        if command != "subscribe" or self._client is None:
            raise ValueError("only SDK subscriptions are supported")
        _, sdk_subscription, listener_base = self._sdk()
        if sdk_subscription is None:
            raise RuntimeError("SDK Subscription class is unavailable")
        subscription = sdk_subscription("MERGE", [params["item"]], params["fields"].split(","))
        subscription.setDataAdapter(params.get("adapter", "Pricing"))
        subscription.addListener(_SubscriptionListener(self, params["LS_subId"], listener_base))
        self._subscriptions[params["LS_subId"]] = subscription
        self._subscription_specs[params["LS_subId"]] = params.copy()
        self._client.subscribe(subscription)

    def receive(self) -> dict[str, Any] | None:
        try:
            return self._queue.get(timeout=0.5)
        except queue.Empty:
            if self._client is None:
                return None
            return {"type": "SDK_HEARTBEAT"}

    def close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
        self._client = None
        self.diagnostics.connection_state = "DISCONNECTED"
        self.diagnostics.client_lifecycle_state = "CLOSED"

    def _on_item_update(self, subscription_id: str, update: Any) -> None:
        spec = self._subscription_specs[subscription_id]
        values = {field: update.getValue(field) for field in spec["fields"].split(",")}
        self.diagnostics.first_updates_received.add(subscription_id)
        self.diagnostics.subscription_states[subscription_id] = "DATA_OBSERVED"
        self._queue.put({**values, "item": update.getItemName() or spec["item"], "subscription_id": subscription_id})

    def _on_subscription(self, subscription_id: str, state: str) -> None:
        self.diagnostics.subscription_states[subscription_id] = state
        if state == "SUBSCRIBED":
            self.diagnostics.subscriptions_accepted.add(subscription_id)


class _ClientListener:
    def __init__(self, transport: OfficialLightstreamerTransport):
        self.transport = transport

    def onStatusChange(self, current_status: str) -> None:
        status = str(current_status)
        if status.startswith("CONNECTING") and self.transport.diagnostics.connection_verified:
            self.transport.diagnostics.reconnect_count += 1
        self.transport.diagnostics.sdk_statuses.append(status)
        self.transport.diagnostics.connection_state = status
        if status.startswith("CONNECTED"):
            self.transport.diagnostics.connection_verified = True
            self.transport.diagnostics.client_lifecycle_state = "CONNECTED"
            self.transport._connection_event.set()
        elif status.startswith("DISCONNECTED"):
            self.transport.diagnostics.client_lifecycle_state = "DISCONNECTED"

    def onServerError(self, error_code: int, error_message: str) -> None:
        safe_message = self.transport._safe_server_error(error_message)
        self.transport.diagnostics.server_error_code = int(error_code)
        self.transport.diagnostics.server_error_message = safe_message
        self.transport.diagnostics.warnings.append(f"SDK server error {error_code}: {safe_message}")
        self.transport._connection_event.set()


class _SubscriptionListener:
    def __init__(self, transport: OfficialLightstreamerTransport, subscription_id: str, _base: Any):
        self.transport = transport
        self.subscription_id = subscription_id

    def onSubscription(self) -> None:
        self.transport._on_subscription(self.subscription_id, "SUBSCRIBED")

    def onUnsubscription(self) -> None:
        self.transport._on_subscription(self.subscription_id, "UNSUBSCRIBED")

    def onItemUpdate(self, update: Any) -> None:
        self.transport._on_item_update(self.subscription_id, update)

    def onSubscriptionError(self, code: int, _message: str) -> None:
        self.transport._on_subscription(self.subscription_id, "ERROR")
        self.transport.diagnostics.warnings.append(f"SDK subscription error {code}")


class IGStreamService:
    """Streaming orchestration; reconnect/recovery belongs to the official SDK."""

    def __init__(self, endpoint: str, username: str, password: str, transport: StreamTransport, *, reconnect_seconds: float = 5.0, max_reconnect_seconds: float = 60.0):
        self.endpoint, self.username, self.password = endpoint, username, password
        self.transport = transport
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self.subscriptions: list[Subscription] = []
        self.stats = StreamStats()
        self._latest_updates: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()

    def add_subscription(self, subscription: Subscription) -> None:
        if subscription not in self.subscriptions:
            self.subscriptions.append(subscription)
            self.stats.updates_received.setdefault(subscription.instrument_id, 0)

    def stop(self, *, reason: str = "requested") -> None:
        self._stop.set()
        self.stats.diagnostics.intentional_shutdown = reason in {"duration", "signal", "requested", "runtime"}
        self.transport.close()

    def mark_duration_expired(self) -> None:
        self.stats.diagnostics.duration_expired = True

    def is_stale(self, instrument_id: str, *, now: float | None = None, threshold: float = 90.0) -> bool:
        last = self.stats.last_update.get(instrument_id)
        return last is not None and (now if now is not None else time.monotonic()) - last > threshold

    def run(self, on_update: Callable[[dict[str, Any]], Any]) -> None:
        try:
            self.transport.connect(self.endpoint, self.username, self.password)
            self._sync_transport_diagnostics()
            for index, subscription in enumerate(self.subscriptions, 1):
                self.transport.send("subscribe", {"LS_subId": str(index), "item": subscription.item, "fields": ",".join(subscription.fields), "adapter": subscription.data_adapter})
                self._sync_transport_diagnostics()
            self.stats.connection_state = "CONNECTED"
            while not self._stop.is_set():
                update = self.transport.receive()
                if update is None:
                    raise ConnectionError("stream disconnected")
                if update.get("type") == "SDK_HEARTBEAT":
                    self._sync_transport_diagnostics()
                    continue
                subscription_id = str(update.get("subscription_id") or "")
                if not subscription_id.isdigit() or not 1 <= int(subscription_id) <= len(self.subscriptions):
                    continue
                subscription = self.subscriptions[int(subscription_id) - 1]
                merged = self._latest_updates.setdefault(subscription.instrument_id, {})
                merged.update(update)
                normalized = dict(merged)
                normalized.update({"instrument_id": subscription.instrument_id, "epic": subscription.item.split(":")[-1]})
                self.stats.updates_received[subscription.instrument_id] += 1
                self.stats.last_update[subscription.instrument_id] = time.monotonic()
                try:
                    on_update(normalized)
                except Exception as exc:
                    failure = {
                        "failure_stage": "observation_callback",
                        "exception_type": type(exc).__name__,
                        "affected_market": subscription.instrument_id,
                    }
                    self.stats.diagnostics.runtime_failures.append(failure)
                    self.stats.warnings.append(
                        "Failure stage={failure_stage}; Exception type={exception_type}; "
                        "Affected market/instrument={affected_market}".format(**failure)
                    )
                self._sync_transport_diagnostics()
        except Exception as exc:
            if not self._stop.is_set():
                self.stats.diagnostics.unexpected_disconnect = True
                transport_diagnostics = getattr(self.transport, "diagnostics", None)
                if transport_diagnostics is not None:
                    transport_diagnostics.unexpected_disconnect = True
                self.stats.connection_state = "FAILED"
                diagnostic = exc.safe_diagnostic() if isinstance(exc, LightstreamerError) else type(exc).__name__
                self.stats.warnings.append(diagnostic)
                log.warning("IG Lightstreamer stopped: %s", diagnostic)
        finally:
            self.transport.close()
            self._sync_transport_diagnostics()
            self.stats.connection_state = "DISCONNECTED"

    def _sync_transport_diagnostics(self) -> None:
        diagnostics = getattr(self.transport, "diagnostics", None)
        if diagnostics is None:
            return
        self.stats.diagnostics = diagnostics
        self.stats.reconnect_count = diagnostics.reconnect_count
        self.stats.warnings.extend(item for item in diagnostics.warnings if item not in self.stats.warnings)
