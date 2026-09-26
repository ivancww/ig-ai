from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

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


class IGStreamService:
    """Reconnectable Lightstreamer orchestration with an injectable transport."""

    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        transport: StreamTransport,
        *,
        reconnect_seconds: float = 5.0,
    ):
        self.endpoint, self.username, self.password = endpoint, username, password
        self.transport = transport
        self.reconnect_seconds = reconnect_seconds
        self.subscriptions: list[Subscription] = []
        self._stop = threading.Event()

    def add_subscription(self, subscription: Subscription) -> None:
        self.subscriptions.append(subscription)

    def stop(self) -> None:
        self._stop.set()
        self.transport.close()

    def run(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        while not self._stop.is_set():
            try:
                self.transport.connect(self.endpoint, self.username, self.password)
                for subscription in self.subscriptions:
                    self.transport.send(
                        "subscribe",
                        {
                            "item": subscription.item,
                            "fields": ",".join(subscription.fields),
                            "adapter": "QUOTE",
                        },
                    )
                while not self._stop.is_set():
                    update = self.transport.receive()
                    if update is None:
                        raise ConnectionError("stream disconnected")
                    on_update(update)
            except Exception:
                if not self._stop.is_set():
                    log.warning("IG stream disconnected; reconnecting")
                    self._stop.wait(self.reconnect_seconds)
            finally:
                try:
                    self.transport.close()
                except Exception:
                    pass


class WebSocketLightstreamerTransport:
    """Optional transport; install the `streaming` extra to use it."""

    def __init__(self):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "Install ig-ai[streaming] to use the Lightstreamer transport"
            ) from exc
        self._websocket = websocket
        self._socket = None

    def connect(self, endpoint: str, username: str, password: str) -> None:
        self._socket = self._websocket.create_connection(endpoint, timeout=30)
        self._socket.send(
            f"LS_op2=create&LS_cid=ig-ai&LS_user={username}&LS_password={password}\r\n"
        )

    def send(self, command: str, params: dict[str, str]) -> None:
        if self._socket is None:
            raise ConnectionError("stream is not connected")
        encoded = "&".join(f"{key}={value}" for key, value in params.items())
        self._socket.send(f"{command}\r\n{encoded}\r\n")

    def receive(self) -> dict[str, Any] | None:
        if self._socket is None:
            return None
        raw = self._socket.recv()
        return {"raw": raw} if raw else None

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
