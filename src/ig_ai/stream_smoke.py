from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .discovery import discover_markets
from .rest import IGRestClient
from .streaming import lightstreamer_password


@dataclass
class SmokeResult:
    authentication: bool = False
    endpoint_received: bool = False
    sdk_client_created: bool = False
    connect_invoked: bool = False
    sdk_statuses: list[str] = field(default_factory=list)
    subscription_requested: bool = False
    subscription_established: bool = False
    item_update_received: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.authentication
            and self.endpoint_received
            and self.sdk_client_created
            and self.connect_invoked
            and any(status.startswith("CONNECTED") for status in self.sdk_statuses)
            and self.subscription_requested
            and self.subscription_established
            and self.item_update_received
        )


class _ConnectionListener:
    def __init__(self, result: SmokeResult, connected: threading.Event):
        self.result = result
        self.connected = connected

    def onStatusChange(self, current_status: str) -> None:
        status = str(current_status)
        self.result.sdk_statuses.append(status)
        if status.startswith("CONNECTED"):
            self.connected.set()

    def onServerError(self, _error_code: int, _error_message: str) -> None:
        # The SDK error is intentionally not retained because it may contain
        # provider or session details.
        pass


class _SubscriptionListener:
    def __init__(self, result: SmokeResult, subscribed: threading.Event, updated: threading.Event):
        self.result = result
        self.subscribed = subscribed
        self.updated = updated

    def onSubscription(self) -> None:
        self.result.subscription_established = True
        self.subscribed.set()

    def onUnsubscription(self) -> None:
        pass

    def onItemUpdate(self, _update: Any) -> None:
        self.result.item_update_received = True
        self.updated.set()

    def onSubscriptionError(self, _code: int, _message: str) -> None:
        pass

    def onClearSnapshot(self, _item_name: str, _item_pos: int) -> None:
        pass

    def onEndOfSnapshot(self, _item_name: str, _item_pos: int) -> None:
        pass


def _sdk() -> tuple[Any, Any]:
    try:
        from lightstreamer.client import LightstreamerClient, Subscription
    except ImportError as exc:
        raise RuntimeError("Install ig-ai[streaming] to use the Lightstreamer SDK") from exc
    return LightstreamerClient, Subscription


def run_stream_smoke(
    client: IGRestClient,
    duration: float,
    *,
    client_factory: Callable[..., Any] | None = None,
    subscription_factory: Callable[..., Any] | None = None,
) -> SmokeResult:
    if duration <= 0:
        raise ValueError("--duration must be positive")

    result = SmokeResult()
    sdk_client = None
    try:
        session_data = client.authenticate()
        result.authentication = True
        session = client.ensure_session()
        endpoint = session.lightstreamer_endpoint or session_data.get("lightstreamerEndpoint")
        account_id = session.account_id or session_data.get("currentAccountId")
        result.endpoint_received = bool(endpoint and account_id)
        if not result.endpoint_received:
            result.error = "missing session streaming details"
            return result

        candidates = discover_markets(client, {"US Tech 100": "US Tech 100"})
        verified = sorted(
            (candidate for candidate in candidates if candidate.verified),
            key=lambda candidate: candidate.epic,
        )
        if not verified:
            result.error = "no verified US Tech 100 weekday cash instrument"
            return result
        epic = verified[0].epic

        if client_factory is None or subscription_factory is None:
            sdk_client_type, subscription_type = _sdk()
            client_factory = client_factory or sdk_client_type
            subscription_factory = subscription_factory or subscription_type

        sdk_client = client_factory(endpoint, "DEFAULT")
        result.sdk_client_created = True
        connected = threading.Event()
        sdk_client.addListener(_ConnectionListener(result, connected))
        sdk_client.setUser(account_id)
        sdk_client.setPassword(lightstreamer_password(session.cst, session.security_token))
        result.connect_invoked = True
        started = time.monotonic()
        sdk_client.connect()
        remaining = max(0.0, duration - (time.monotonic() - started))
        if not connected.wait(remaining):
            result.error = "SDK did not reach CONNECTED"
            return result

        subscribed = threading.Event()
        updated = threading.Event()
        subscription = subscription_factory(
            "MERGE",
            [f"PRICE:{account_id}:{epic}"],
            ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG"],
        )
        subscription.setDataAdapter("Pricing")
        subscription.addListener(_SubscriptionListener(result, subscribed, updated))
        result.subscription_requested = True
        sdk_client.subscribe(subscription)
        remaining = max(0.0, duration - (time.monotonic() - started))
        if not subscribed.wait(remaining):
            result.error = "subscription was not established"
            return result
        if not updated.wait(max(0.0, duration - (time.monotonic() - started))):
            result.error = "no ItemUpdate received"
    except Exception as exc:
        result.error = type(exc).__name__
    finally:
        if sdk_client is not None:
            try:
                sdk_client.disconnect()
            except Exception:
                pass
    return result


def format_smoke_result(result: SmokeResult) -> str:
    lines = [
        f"Authentication: {'PASS' if result.authentication else 'FAIL'}",
        f"Endpoint received: {'YES' if result.endpoint_received else 'NO'}",
        f"SDK client created: {'YES' if result.sdk_client_created else 'NO'}",
        f"connect() invoked: {'YES' if result.connect_invoked else 'NO'}",
    ]
    lines.extend(f"SDK status: {status}" for status in result.sdk_statuses)
    lines.extend(
        [
            f"Subscription requested: {'YES' if result.subscription_requested else 'NO'}",
            f"Subscription established: {'YES' if result.subscription_established else 'NO'}",
            f"ItemUpdate received: {'YES' if result.item_update_received else 'NO'}",
            f"LIVE SMOKE TEST: {'PASS' if result.passed else 'FAIL'}",
        ]
    )
    return "\n".join(lines)
