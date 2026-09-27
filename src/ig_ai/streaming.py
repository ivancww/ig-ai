from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

LIGHTSTREAMER_PROTOCOL = "TLCP-2.4.0"
LIGHTSTREAMER_SUBPROTOCOL = "TLCP-2.4.0.lightstreamer.com"
LIGHTSTREAMER_CLIENT_ID = "mgQkwtwdysogQz2BJ4Ji kOj2Bg"


def lightstreamer_ws_endpoint(endpoint: str) -> str:
    """Build the documented WS endpoint without retaining endpoint query data."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("Lightstreamer endpoint must be an absolute HTTP(S) URL")
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}[parsed.scheme]
    path = parsed.path.rstrip("/")
    if not path.endswith("/lightstreamer"):
        path += "/lightstreamer"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


class LightstreamerError(ConnectionError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        phase: str,
        status: int | None = None,
        provider_error: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.endpoint = _safe_endpoint(endpoint)
        self.phase = phase
        self.status = status
        self.provider_error = provider_error
        self.retryable = retryable

    def safe_diagnostic(self) -> str:
        return (
            "Lightstreamer diagnostic: "
            f"status={self.status if self.status is not None else 'none'} "
            f"phase={self.phase} endpoint={self.endpoint} "
            f"provider_error={self.provider_error or 'none'}"
        )


class StreamTransport(Protocol):
    def connect(self, endpoint: str, username: str, password: str) -> None: ...
    def send(self, command: str, params: dict[str, str]) -> None: ...
    def receive(self) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Subscription:
    instrument_id: str
    item: str
    fields: tuple[str, ...] = ("BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG")
    data_adapter: str = "Pricing"


@dataclass
class StreamStats:
    connection_state: str = "DISCONNECTED"
    reconnect_count: int = 0
    updates_received: dict[str, int] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    diagnostics: StreamDiagnostics = field(default_factory=lambda: StreamDiagnostics())


@dataclass
class StreamDiagnostics:
    """Safe lifecycle evidence; values never contain authenticated frame data."""

    websocket_handshake_accepted: bool = False
    wsok_received: bool = False
    create_session_sent: bool = False
    conok_received: bool = False
    session_id_established: bool = False
    control_sent: bool = False
    subscription_requests_sent: dict[str, str] = field(default_factory=dict)
    subscriptions_accepted: set[str] = field(default_factory=set)
    first_updates_received: set[str] = field(default_factory=set)
    server_messages: list[str] = field(default_factory=list)
    protocol_errors: list[str] = field(default_factory=list)
    socket_close_code: int | None = None
    socket_close_reason: str | None = None
    intentional_shutdown: bool = False
    duration_expired: bool = False
    unexpected_disconnect: bool = False


def lightstreamer_password(cst: str, security_token: str) -> str:
    return f"CST-{cst}|XST-{security_token}"


class IGStreamService:
    """Reconnectable Lightstreamer orchestration with an injectable transport."""

    def __init__(self, endpoint: str, username: str, password: str, transport: StreamTransport, *, reconnect_seconds: float = 5.0, max_reconnect_seconds: float = 60.0):
        self.endpoint, self.username, self.password = endpoint, username, password
        self.transport = transport
        self.reconnect_seconds = max(0.0, reconnect_seconds)
        self.max_reconnect_seconds = max(self.reconnect_seconds, max_reconnect_seconds)
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
        self.stats.diagnostics.intentional_shutdown = reason in {"duration", "signal", "requested"}
        self.stats.connection_state = "DISCONNECTED"
        self.transport.close()
        self._sync_transport_diagnostics()

    def mark_duration_expired(self) -> None:
        self.stats.diagnostics.duration_expired = True

    def is_stale(self, instrument_id: str, *, now: float | None = None, threshold: float = 90.0) -> bool:
        last = self.stats.last_update.get(instrument_id)
        return last is not None and (now if now is not None else time.monotonic()) - last > threshold

    def run(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        delay = self.reconnect_seconds
        while not self._stop.is_set():
            try:
                self.stats.connection_state = "CONNECTING"
                self.transport.connect(self.endpoint, self.username, self.password)
                self.stats.diagnostics.websocket_handshake_accepted = True
                self._sync_transport_diagnostics()
                for index, subscription in enumerate(self.subscriptions, 1):
                    self.transport.send(
                        "subscribe",
                        {
                            "item": subscription.item,
                            "fields": ",".join(subscription.fields),
                            "adapter": subscription.data_adapter,
                            "LS_subId": str(index),
                        },
                    )
                    self.stats.diagnostics.subscription_requests_sent[str(index)] = subscription.item
                    self._sync_transport_diagnostics()
                self.stats.connection_state = "CONNECTED"
                delay = self.reconnect_seconds
                while not self._stop.is_set():
                    update = self.transport.receive()
                    if update is None:
                        raise ConnectionError("stream disconnected")
                    self._sync_transport_diagnostics()
                    if update.get("type") in {
                        "PROBE", "SUB", "UNSUB", "LOOP", "PROG", "SYNC", "CONF", "CONS", "REQOK",
                        "SUBCMD",
                    }:
                        continue
                    item = str(update.get("item") or update.get("epic") or "")
                    for index, subscription in enumerate(self.subscriptions, 1):
                        if item in {subscription.item, subscription.instrument_id, subscription.item.split(":")[-1], f"ITEM{index}"}:
                            merged = self._latest_updates.setdefault(subscription.instrument_id, {})
                            merged.update({key: value for key, value in update.items() if value not in (None, "")})
                            if not any(key in update for key in ("UPDATE_TIME", "UPDATE_TIMESTAMP", "PROVIDER_TIMESTAMP", "timestamp")):
                                merged["timestamp"] = datetime.now(UTC).isoformat()
                            update = dict(merged)
                            update["item"] = item
                            self.stats.updates_received[subscription.instrument_id] += 1
                            self.stats.diagnostics.first_updates_received.add(str(index))
                            self.stats.last_update[subscription.instrument_id] = time.monotonic()
                            update.setdefault("instrument_id", subscription.instrument_id)
                            update.setdefault("epic", subscription.item.split(":")[-1])
                            break
                    on_update(update)
            except Exception as exc:
                self._sync_transport_diagnostics()
                if not self._stop.is_set():
                    self.stats.diagnostics.unexpected_disconnect = True
                    diagnostic = exc.safe_diagnostic() if isinstance(exc, LightstreamerError) else type(exc).__name__
                    self.stats.warnings.append(diagnostic)
                    if isinstance(exc, LightstreamerError) and not exc.retryable:
                        self.stats.connection_state = "FAILED"
                        log.warning("IG Lightstreamer stopped: %s", diagnostic)
                        self._stop.set()
                    else:
                        self.stats.connection_state = "RECONNECTING"
                        self.stats.reconnect_count += 1
                        log.warning("IG Lightstreamer disconnected; reconnecting: %s", diagnostic)
                        self._stop.wait(delay)
                        delay = min(self.max_reconnect_seconds, max(0.1, delay * 2 or 0.1))
            finally:
                try:
                    self.transport.close()
                except Exception:
                    pass
                self._sync_transport_diagnostics()
        self.stats.connection_state = "DISCONNECTED"

    def _sync_transport_diagnostics(self) -> None:
        diagnostics = getattr(self.transport, "diagnostics", None)
        if diagnostics is None:
            return
        current = self.stats.diagnostics
        for name in (
            "websocket_handshake_accepted", "wsok_received", "create_session_sent",
            "conok_received", "session_id_established", "control_sent", "intentional_shutdown",
            "duration_expired", "unexpected_disconnect", "socket_close_code",
            "socket_close_reason",
        ):
            value = getattr(diagnostics, name, None)
            if value is not None and (value is True or value is not False):
                setattr(current, name, value)
        current.subscription_requests_sent.update(diagnostics.subscription_requests_sent)
        current.subscriptions_accepted.update(diagnostics.subscriptions_accepted)
        current.first_updates_received.update(diagnostics.first_updates_received)
        for target, source in ((current.server_messages, diagnostics.server_messages), (current.protocol_errors, diagnostics.protocol_errors)):
            for value in source:
                if value not in target:
                    target.append(value)


class WebSocketLightstreamerTransport:
    """Lightstreamer text protocol transport for IG's authenticated endpoint."""

    def __init__(self):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("Install ig-ai[streaming] to use the Lightstreamer transport") from exc
        self._websocket = websocket
        self._socket = None
        self._field_names: list[str] = []
        self._endpoint = ""
        self._session_id: str | None = None
        self._next_request_id = 1
        self.diagnostics = StreamDiagnostics()
        self._secrets: tuple[str, ...] = ()

    def connect(self, endpoint: str, username: str, password: str) -> None:
        self._endpoint = lightstreamer_ws_endpoint(endpoint)
        # Request IDs are unique within a WebSocket connection. A fresh
        # connection creates a fresh TLCP session and starts at one.
        self._session_id = None
        self._next_request_id = 1
        self.diagnostics = StreamDiagnostics()
        self._secrets = (username, password)
        try:
            self._socket = self._websocket.create_connection(
                self._endpoint,
                timeout=30,
                subprotocols=[LIGHTSTREAMER_SUBPROTOCOL],
            )
            self.diagnostics.websocket_handshake_accepted = True
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            retryable = status is None or status >= 500
            raise LightstreamerError(
                "Lightstreamer WebSocket handshake failed",
                endpoint=self._endpoint,
                phase="websocket_handshake",
                status=status,
                retryable=retryable,
            ) from exc
        self._send_request("wsok")
        self._expect("WSOK", "websocket_check")
        self.diagnostics.wsok_received = True
        self.diagnostics.create_session_sent = True
        self._send_request(
            "create_session",
            {
                "LS_cid": LIGHTSTREAMER_CLIENT_ID,
                "LS_user": username,
                "LS_password": password,
                "LS_send_sync": "false",
            },
        )
        while True:
            message = self._recv("session_creation")
            if not message:
                raise LightstreamerError(
                    "Lightstreamer session closed during setup",
                    endpoint=self._endpoint,
                    phase="session_creation",
                )
            tag, args = self._response(str(message))
            if tag == "CONOK":
                self._session_id = args[0] if args else None
                self.diagnostics.conok_received = True
                self.diagnostics.session_id_established = bool(self._session_id)
                return
            if tag in {"CONERR", "ERROR", "REQERR"}:
                self._raise_protocol(tag, args, "session_creation")

    def send(self, command: str, params: dict[str, str]) -> None:
        if self._socket is None:
            raise ConnectionError("stream is not connected")
        if self._session_id is None:
            raise ConnectionError("Lightstreamer session is not established")
        if command == "subscribe":
            self._field_names = params["fields"].split(",")
            params = {
                "LS_op": "add",
                "LS_reqId": self._request_id(),
                "LS_subId": params["LS_subId"],
                "LS_group": params["item"],
                "LS_schema": params["fields"],
                "LS_data_adapter": params.get("adapter", "Pricing"),
                "LS_mode": "MERGE",
            }
            params["LS_session"] = self._session_id
            self.diagnostics.subscription_requests_sent[params["LS_subId"]] = params["LS_group"]
            self._send_request("control", params)
            self.diagnostics.control_sent = True
            return
        raise ValueError(f"unsupported Lightstreamer command: {command}")

    def _request_id(self) -> str:
        request_id = str(self._next_request_id)
        self._next_request_id += 1
        return request_id

    def receive(self) -> dict[str, Any] | None:
        if self._socket is None:
            return None
        raw = self._socket.recv()
        return self._parse(str(raw)) if raw else None

    def _parse(self, raw: str) -> dict[str, Any]:
        self._ensure_diagnostics()
        line = raw.strip("\r\n")
        if line.startswith("PROBE"):
            self._record_server_message("PROBE")
            return {"type": "PROBE"}
        tag, args = self._response(line)
        if tag in {"ERROR", "END", "CONERR", "REQERR"}:
            self._record_server_message(tag)
            self._raise_protocol(tag, args, "stream")
        if tag == "SUBOK":
            self._record_server_message(tag)
            if args:
                self.diagnostics.subscriptions_accepted.add(args[0])
            return {"type": "SUB", "subscription_id": args[0] if args else ""}
        if tag == "UNSUBOK":
            self._record_server_message(tag)
            return {"type": "UNSUB"}
        if tag == "U":
            if len(args) < 3:
                return {"type": "U"}
            self.diagnostics.first_updates_received.add(args[0])
        elif tag in {"LOOP", "PROG", "SYNC", "CONF", "CONS", "REQOK", "SUBCMD"}:
            self._record_server_message(tag)
            return {"type": tag}
        if tag != "U" or len(args) < 3:
            return {"type": tag or "UNKNOWN"}
        subscription_id, item, values = args[0], args[1], args[2]
        update: dict[str, Any] = {"item": item, "subscription_id": subscription_id}
        for index, value in enumerate(values.split("|")):
            if index < len(self._field_names) and value != "":
                if value.startswith("^"):
                    continue
                update[self._field_names[index]] = value
        return update

    def _send_request(self, name: str, params: dict[str, str] | None = None) -> None:
        encoded = urlencode(params or {})
        self._socket.send(name + ("\r\n" + encoded if params is not None else ""))

    def _recv(self, phase: str) -> str:
        try:
            raw = self._socket.recv()
        except Exception as exc:
            raise LightstreamerError(
                "Lightstreamer receive failed", endpoint=self._endpoint, phase=phase
            ) from exc
        return str(raw) if raw else ""

    def _expect(self, expected: str, phase: str) -> None:
        response = self._recv(phase).strip("\r\n")
        if response != expected:
            tag, args = self._response(response)
            self._raise_protocol(tag or "UNKNOWN", args, phase)

    @staticmethod
    def _response(line: str) -> tuple[str, list[str]]:
        parts = line.split(",", 1)
        return parts[0], parts[1].split(",") if len(parts) == 2 else []

    def _raise_protocol(self, tag: str, args: list[str], phase: str) -> None:
        self._ensure_diagnostics()
        # Keep only the provider error code. Messages can echo request values.
        provider_error = self._safe_protocol_text(args[-2][:160] if len(args) >= 2 else tag)
        self.diagnostics.protocol_errors.append(tag)
        raise LightstreamerError(
            f"Lightstreamer protocol response {tag}",
            endpoint=getattr(self, "_endpoint", ""),
            phase=phase,
            provider_error=provider_error,
        )

    def _record_server_message(self, tag: str) -> None:
        if tag not in self.diagnostics.server_messages:
            self.diagnostics.server_messages.append(tag)

    def _safe_protocol_text(self, value: str) -> str:
        safe = value
        for secret in self._secrets:
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        safe = re.sub(r"(?i)(CST|XST)-[^|,\s]+", r"\1-[REDACTED]", safe)
        safe = re.sub(r"(?i)(authorization|access[_-]?token|refresh[_-]?token|LS_password)=[^&,\s]+", r"\1=[REDACTED]", safe)
        return safe

    def _ensure_diagnostics(self) -> None:
        if not hasattr(self, "diagnostics"):
            self.diagnostics = StreamDiagnostics()
        if not hasattr(self, "_secrets"):
            self._secrets = ()

    def close(self) -> None:
        if self._socket is not None:
            code = getattr(self._socket, "close_status_code", None)
            reason = getattr(self._socket, "close_reason", None)
            if code is not None:
                self.diagnostics.socket_close_code = code
            if reason:
                self.diagnostics.socket_close_reason = self._safe_protocol_text(str(reason)[:160])
            self._socket.close()
            self._socket = None
        self._session_id = None
