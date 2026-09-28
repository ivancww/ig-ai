from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

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
    control_requests: dict[str, str] = field(default_factory=dict)
    subscription_requests_sent: dict[str, str] = field(default_factory=dict)
    reqok_request_ids: set[str] = field(default_factory=set)
    reqerr_request_ids: set[str] = field(default_factory=set)
    subok_subscription_ids: set[str] = field(default_factory=set)
    subcmd_subscription_ids: set[str] = field(default_factory=set)
    subscriptions_accepted: set[str] = field(default_factory=set)
    first_updates_received: set[str] = field(default_factory=set)
    u_messages_received: set[str] = field(default_factory=set)
    safe_update_diagnostics: list[dict[str, str]] = field(default_factory=list)
    subscription_states: dict[str, str] = field(default_factory=dict)
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
                    subscription_id = str(update.get("subscription_id") or "")
                    matched = None
                    if subscription_id.isdigit():
                        subscription_index = int(subscription_id)
                        item_is_valid = not item.isdigit() or item == "1"
                        if item_is_valid and 1 <= subscription_index <= len(self.subscriptions):
                            matched = (subscription_index, self.subscriptions[subscription_index - 1])
                    if matched is None:
                        for index, subscription in enumerate(self.subscriptions, 1):
                            if item in {subscription.item, subscription.instrument_id, subscription.item.split(":")[-1], f"ITEM{index}"}:
                                matched = (index, subscription)
                                break
                    if matched is None:
                        on_update(update)
                        continue
                    index, subscription = matched
                    merged = self._latest_updates.setdefault(subscription.instrument_id, {})
                    merged.update({key: value for key, value in update.items() if key not in {"item", "subscription_id"}})
                    update = dict(merged)
                    update["item"] = item
                    update["subscription_id"] = subscription_id
                    update["instrument_id"] = subscription.instrument_id
                    update["epic"] = subscription.item.split(":")[-1]
                    self.stats.updates_received[subscription.instrument_id] += 1
                    bid = update.get("BID") or update.get("BIDPRICE1")
                    ask = update.get("OFFER") or update.get("ASKPRICE1")
                    if bid not in (None, "") and ask not in (None, ""):
                        self.stats.diagnostics.first_updates_received.add(str(index))
                        self.stats.diagnostics.subscription_states[subscription_id] = "DATA_OBSERVED"
                    self.stats.last_update[subscription.instrument_id] = time.monotonic()
                    result = on_update(update)
                    self._sync_transport_diagnostics()
                    for diagnostic in reversed(self.stats.diagnostics.safe_update_diagnostics):
                        if (
                            diagnostic.get("subscription_id") == subscription_id
                            and diagnostic.get("item_index") == item
                            and diagnostic.get("observation_created") == "false"
                        ):
                            if isinstance(result, dict):
                                diagnostic.update(result)
                            break
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
        current.control_requests.update(diagnostics.control_requests)
        current.reqok_request_ids.update(diagnostics.reqok_request_ids)
        current.reqerr_request_ids.update(diagnostics.reqerr_request_ids)
        current.subok_subscription_ids.update(diagnostics.subok_subscription_ids)
        current.subcmd_subscription_ids.update(diagnostics.subcmd_subscription_ids)
        current.subscriptions_accepted.update(diagnostics.subscriptions_accepted)
        current.first_updates_received.update(diagnostics.first_updates_received)
        current.u_messages_received.update(diagnostics.u_messages_received)
        current.safe_update_diagnostics.extend(
            item for item in diagnostics.safe_update_diagnostics if item not in current.safe_update_diagnostics
        )
        current.subscription_states.update(diagnostics.subscription_states)
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
        self._field_names_by_subscription: dict[str, tuple[str, ...]] = {}
        self._market_identity_by_subscription: dict[str, str] = {}
        self._field_state: dict[tuple[str, str], dict[str, Any]] = {}
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
        self._field_names_by_subscription = {}
        self._market_identity_by_subscription = {}
        self._field_state = {}
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
            request_id = self._request_id()
            subscription_id = params["LS_subId"]
            self._field_names_by_subscription[subscription_id] = tuple(self._field_names)
            self._market_identity_by_subscription[subscription_id] = params["item"].split(":")[-1]
            params = {
                "LS_op": "add",
                "LS_reqId": request_id,
                "LS_subId": subscription_id,
                "LS_group": params["item"],
                "LS_schema": params["fields"],
                "LS_data_adapter": params.get("adapter", "Pricing"),
                "LS_mode": "MERGE",
            }
            params["LS_session"] = self._session_id
            self.diagnostics.control_requests[request_id] = subscription_id
            self.diagnostics.subscription_requests_sent[subscription_id] = params["LS_group"]
            self.diagnostics.subscription_states[subscription_id] = "CONTROL_SENT"
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
        if not hasattr(self, "_field_names_by_subscription"):
            self._field_names_by_subscription = {}
        if not hasattr(self, "_market_identity_by_subscription"):
            self._market_identity_by_subscription = {}
        if not hasattr(self, "_field_state"):
            self._field_state = {}
        line = raw.strip("\r\n")
        if line.startswith("PROBE"):
            self._record_server_message("PROBE")
            return {"type": "PROBE"}
        tag, args = self._response(line)
        if tag == "REQOK":
            request_id = args[0] if args else ""
            self._record_server_message(tag)
            if request_id:
                self.diagnostics.reqok_request_ids.add(request_id)
                subscription_id = self.diagnostics.control_requests.get(request_id)
                if subscription_id:
                    self.diagnostics.subscription_states[subscription_id] = "CONTROL_ACCEPTED"
            return {
                "type": tag,
                "request_id": request_id,
                "subscription_id": self.diagnostics.control_requests.get(request_id, ""),
            }
        if tag == "REQERR":
            request_id = args[0] if args else ""
            self._record_server_message(tag)
            if request_id:
                self.diagnostics.reqerr_request_ids.add(request_id)
            self._raise_protocol(tag, args, "stream")
        if tag in {"ERROR", "END", "CONERR"}:
            self._record_server_message(tag)
            self._raise_protocol(tag, args, "stream")
        if tag == "SUBOK":
            self._record_server_message(tag)
            subscription_id = args[0] if args else ""
            if subscription_id in self.diagnostics.subscription_requests_sent:
                self.diagnostics.subok_subscription_ids.add(subscription_id)
                self.diagnostics.subscriptions_accepted.add(subscription_id)
                self.diagnostics.subscription_states[subscription_id] = "SUBSCRIPTION_ESTABLISHED"
            return {"type": "SUB", "subscription_id": subscription_id}
        if tag == "UNSUBOK":
            self._record_server_message(tag)
            return {"type": "UNSUB"}
        if tag == "U":
            if len(args) < 3:
                subscription_id = args[0] if args else ""
                self._record_update_diagnostic(
                    subscription_id,
                    args[1] if len(args) > 1 else "",
                    (),
                    {},
                    "malformed_u_frame",
                    field_count_received=max(0, len(args) - 2),
                    previous_state_available=False,
                )
                return {"type": "U"}
            subscription_id = args[0]
            if subscription_id in self.diagnostics.subscription_requests_sent:
                self.diagnostics.u_messages_received.add(subscription_id)
        elif tag == "SUBCMD":
            self._record_server_message(tag)
            subscription_id = args[0] if args else ""
            if subscription_id in self.diagnostics.subscription_requests_sent:
                self.diagnostics.subcmd_subscription_ids.add(subscription_id)
                self.diagnostics.subscriptions_accepted.add(subscription_id)
                self.diagnostics.subscription_states[subscription_id] = "SUBSCRIPTION_ESTABLISHED"
            return {"type": tag, "subscription_id": subscription_id}
        elif tag in {"LOOP", "PROG", "SYNC", "CONF", "CONS"}:
            self._record_server_message(tag)
            return {"type": tag}
        if tag != "U" or len(args) < 3:
            return {"type": tag or "UNKNOWN"}
        subscription_id, item, encoded_values = args[0], unquote(args[1]), args[2]
        field_names = getattr(self, "_field_names_by_subscription", {}).get(subscription_id, tuple(self._field_names))
        field_state = getattr(self, "_field_state", {})
        state = field_state.setdefault((subscription_id, item), {})
        previous_state_available = bool(state)
        encoded_values_list, received_values, percent_escape_count, compression_marker_count = (
            self._decode_u_field_list(encoded_values, len(field_names))
        )
        changed_fields: list[str] = []
        field_index = 0
        for value in received_values:
            if field_index >= len(field_names):
                break
            if value == "":
                field_index += 1
                continue
            if value.startswith("^") and len(value) > 1 and value[1:].isdigit():
                field_index += int(value[1:])
                continue
            field_name = field_names[field_index]
            if value == "#":
                state[field_name] = None
            elif value == "$":
                state[field_name] = ""
            elif value.startswith("^"):
                self._record_update_diagnostic(
                    subscription_id,
                    item,
                    changed_fields,
                    state,
                    "unsupported_field_diff",
                    field_count_received=len(received_values),
                    previous_state_available=previous_state_available,
                )
                return {"type": "U", "subscription_id": subscription_id, "item": item}
            else:
                state[field_name] = value
            changed_fields.append(field_name)
            field_index += 1
        update: dict[str, Any] = {"item": item, "subscription_id": subscription_id, **state}
        bid = state.get("BID") or state.get("BIDPRICE1")
        ask = state.get("OFFER") or state.get("ASKPRICE1")
        sufficient = bid not in (None, "") and ask not in (None, "")
        self._record_update_diagnostic(
            subscription_id,
            item,
            changed_fields,
            state,
            "none" if sufficient else "missing_bid_or_ask",
            field_count_received=len(received_values),
            previous_state_available=previous_state_available,
            u_argument_count=len(args),
            encoded_field_token_count=len(encoded_values_list),
            decoded_field_count=len(received_values),
            final_argument_length=len(encoded_values),
            percent_escape_count=percent_escape_count,
            compression_marker_count=compression_marker_count,
        )
        return update

    def _record_update_diagnostic(
        self,
        subscription_id: str,
        item: str,
        fields: list[str],
        state: dict[str, Any],
        skip_reason: str,
        *,
        field_count_received: int = 0,
        previous_state_available: bool = False,
        u_argument_count: int = 0,
        encoded_field_token_count: int = 0,
        decoded_field_count: int = 0,
        final_argument_length: int = 0,
        percent_escape_count: int = 0,
        compression_marker_count: int = 0,
    ) -> None:
        field_names = getattr(self, "_field_names_by_subscription", {}).get(subscription_id, tuple(self._field_names))
        decoded_names = [name for name in field_names if state.get(name) not in (None, "")]
        bid_present = state.get("BIDPRICE1") not in (None, "") or state.get("BID") not in (None, "")
        ask_present = state.get("ASKPRICE1") not in (None, "") or state.get("OFFER") not in (None, "")
        timestamp_present = any(
            state.get(name) not in (None, "") for name in ("TIMESTAMP", "UPDATE_TIME", "UTM")
        )
        self.diagnostics.safe_update_diagnostics.append(
            {
                "market_identity": getattr(self, "_market_identity_by_subscription", {}).get(
                    subscription_id, item
                ),
                "subscription_id": subscription_id,
                "item_index": item,
                "schema_fields_expected": ",".join(field_names),
                "u_argument_count": str(u_argument_count),
                "encoded_field_token_count": str(encoded_field_token_count),
                "decoded_field_count": str(decoded_field_count),
                "field_count_received": str(field_count_received),
                "final_argument_length": str(final_argument_length),
                "percent_escape_count": str(percent_escape_count),
                "compression_marker_count": str(compression_marker_count),
                "decoded_field_names_present": ",".join(decoded_names),
                "fields_changed": ",".join(fields),
                "bid_present": str(bid_present).lower(),
                "ask_present": str(ask_present).lower(),
                "timestamp_present": str(timestamp_present).lower(),
                "previous_state_available": str(previous_state_available).lower(),
                "observation_created": "false",
                "skip_reason": skip_reason,
            }
        )

    @staticmethod
    def _decode_u_field_list(
        encoded_values: str, expected_field_count: int
    ) -> tuple[list[str], list[str], int, int]:
        """Decode TLCP's variable-length final U argument safely.

        The normal TLCP form contains literal pipes. Some IG websocket paths
        apply an additional URL-encoding layer to the complete final argument,
        so a structural pipe can arrive as ``%257C``. Decode only enough layers
        to expose separators, and use the subscribed schema cardinality to
        avoid mistaking a percent-encoded pipe inside one field for a boundary.
        """
        encoded_field_tokens = encoded_values.split("|")
        candidate = encoded_values
        candidate_tokens = encoded_field_tokens
        for _ in range(3):
            if "|" in candidate:
                break
            decoded_candidate = unquote(candidate)
            if decoded_candidate == candidate:
                break
            candidate = decoded_candidate
            candidate_tokens = candidate.split("|")
            if len(candidate_tokens) > 1 and len(candidate_tokens) <= expected_field_count:
                break

        if len(candidate_tokens) > expected_field_count and len(encoded_field_tokens) == 1:
            candidate = encoded_values
            candidate_tokens = encoded_field_tokens

        received_values = [unquote(value) for value in candidate_tokens]
        percent_escape_count = len(re.findall(r"%[0-9A-Fa-f]{2}", encoded_values))
        compression_marker_count = sum(
            1 for value in received_values if value.startswith("^")
        )
        return (
            encoded_field_tokens,
            received_values,
            percent_escape_count,
            compression_marker_count,
        )

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
        if len(parts) != 2:
            return parts[0], []
        if parts[0] == "U":
            # U has exactly three arguments. The final field-list argument is
            # variable-length and may itself contain commas.
            return parts[0], parts[1].split(",", 2)
        return parts[0], parts[1].split(",")

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
