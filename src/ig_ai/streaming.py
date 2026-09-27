from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)


class StreamTransport(Protocol):
    def connect(self, endpoint: str, username: str, password: str) -> None: ...
    def send(self, command: str, params: dict[str, str]) -> None: ...
    def receive(self) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Subscription:
    instrument_id: str
    item: str
    fields: tuple[str, ...] = ("BID", "OFFER", "UPDATE_TIME", "MARKET_STATE")


@dataclass
class StreamStats:
    connection_state: str = "DISCONNECTED"
    reconnect_count: int = 0
    updates_received: dict[str, int] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def lightstreamer_password(cst: str, security_token: str) -> str:
    return f"CST-{cst}|X-SECURITY-TOKEN-{security_token}"


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

    def stop(self) -> None:
        self._stop.set()
        self.stats.connection_state = "DISCONNECTED"
        self.transport.close()

    def is_stale(self, instrument_id: str, *, now: float | None = None, threshold: float = 90.0) -> bool:
        last = self.stats.last_update.get(instrument_id)
        return last is not None and (now if now is not None else time.monotonic()) - last > threshold

    def run(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        delay = self.reconnect_seconds
        while not self._stop.is_set():
            try:
                self.stats.connection_state = "CONNECTING"
                self.transport.connect(self.endpoint, self.username, self.password)
                for index, subscription in enumerate(self.subscriptions, 1):
                    self.transport.send("subscribe", {"item": subscription.item, "fields": ",".join(subscription.fields), "adapter": "QUOTE", "LS_subId": str(index)})
                self.stats.connection_state = "CONNECTED"
                delay = self.reconnect_seconds
                while not self._stop.is_set():
                    update = self.transport.receive()
                    if update is None:
                        raise ConnectionError("stream disconnected")
                    if update.get("type") in {"PROBE", "SUB", "UNSUB"}:
                        on_update(update)
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
                            self.stats.last_update[subscription.instrument_id] = time.monotonic()
                            update.setdefault("instrument_id", subscription.instrument_id)
                            update.setdefault("epic", subscription.item.split(":")[-1])
                            break
                    on_update(update)
            except Exception as exc:
                if not self._stop.is_set():
                    self.stats.connection_state = "RECONNECTING"
                    self.stats.reconnect_count += 1
                    self.stats.warnings.append(type(exc).__name__)
                    log.warning("IG Lightstreamer disconnected; reconnecting")
                    self._stop.wait(delay)
                    delay = min(self.max_reconnect_seconds, max(0.1, delay * 2 or 0.1))
            finally:
                try:
                    self.transport.close()
                except Exception:
                    pass
        self.stats.connection_state = "DISCONNECTED"


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

    def connect(self, endpoint: str, username: str, password: str) -> None:
        self._socket = self._websocket.create_connection(endpoint, timeout=30)
        self._socket.send("LS_op2=create&LS_cid=ig-ai&LS_adapter_set=QUOTE" f"&LS_user={quote(username)}&LS_password={quote(password)}\r\n")
        while True:
            message = self._socket.recv()
            if not message:
                raise ConnectionError("Lightstreamer session closed during setup")
            line = str(message).strip()
            if line.startswith("CONOK"):
                return
            if line.startswith("ERROR"):
                raise ConnectionError("Lightstreamer session setup failed")

    def send(self, command: str, params: dict[str, str]) -> None:
        if self._socket is None:
            raise ConnectionError("stream is not connected")
        if command == "subscribe":
            params = {
                "LS_op": "add",
                "LS_subId": params["LS_subId"],
                "LS_group": params["item"],
                "LS_schema": params["fields"],
                "LS_data_adapter": params.get("adapter", "QUOTE"),
                "LS_mode": "MERGE",
            }
        encoded = "&".join(f"{quote(key)}={quote(value)}" for key, value in params.items())
        self._socket.send(f"{command.upper()}\r\n{encoded}\r\n")

    def receive(self) -> dict[str, Any] | None:
        if self._socket is None:
            return None
        raw = self._socket.recv()
        return self._parse(str(raw)) if raw else None

    def _parse(self, raw: str) -> dict[str, Any]:
        line = raw.strip("\r\n")
        if line.startswith("PROBE"):
            return {"type": "PROBE"}
        if line.startswith("ERROR"):
            raise ConnectionError("Lightstreamer reported a stream error")
        if line.startswith("SUB|"):
            parts = line.split("|")
            self._field_names = parts[4:]
            return {"type": "SUB", "item": parts[3] if len(parts) > 3 else ""}
        if line.startswith("UNSUB"):
            return {"type": "UNSUB"}
        parts = line.split("|")
        if not parts or not parts[0].startswith("ITEM"):
            return {"type": parts[0] if parts else "UNKNOWN"}
        update: dict[str, Any] = {"item": parts[0]}
        for index, value in enumerate(parts[1:]):
            if index < len(self._field_names) and value != "":
                update[self._field_names[index]] = value
        return update

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
