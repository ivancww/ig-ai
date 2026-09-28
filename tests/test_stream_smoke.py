from __future__ import annotations

import sys
import threading

from ig_ai.rest import IGSession
from ig_ai.stream_smoke import format_smoke_result, run_stream_smoke


class FakeRestClient:
    def __init__(self):
        self.session = IGSession("cst", "token", "https://stream.example", "ACCOUNT")

    def authenticate(self):
        return {"lightstreamerEndpoint": self.session.lightstreamer_endpoint, "currentAccountId": "ACCOUNT"}

    def ensure_session(self):
        return self.session


class FakeSDKSubscription:
    created = []

    def __init__(self, mode, items, fields):
        self.mode = mode
        self.items = items
        self.fields = fields
        self.adapter = None
        self.listener = None
        self.created.append(self)

    def setDataAdapter(self, adapter):
        self.adapter = adapter

    def addListener(self, listener):
        self.listener = listener


class FakeSDKClient:
    def __init__(self, endpoint, adapter_set):
        self.endpoint = endpoint
        self.adapter_set = adapter_set
        self.listener = None
        self.events = []
        self.subscription = None

    def setUser(self, user):
        self.events.append(("user", user))

    def setPassword(self, password):
        self.events.append(("password", password))

    def addListener(self, listener):
        self.events.append(("listener",))
        self.listener = listener

    def connect(self):
        self.events.append(("connect",))
        self.listener.onStatusChange("CONNECTING")
        threading.Timer(0.01, lambda: self.listener.onStatusChange("CONNECTED:STREAM-SENSING")).start()

    def subscribe(self, subscription):
        self.subscription = subscription
        threading.Timer(0.01, subscription.listener.onSubscription).start()
        threading.Timer(0.02, lambda: subscription.listener.onItemUpdate(object())).start()

    def disconnect(self):
        self.events.append(("disconnect",))


def fake_discovery(monkeypatch):
    from ig_ai import stream_smoke
    from ig_ai.discovery import DiscoveryCandidate

    monkeypatch.setattr(
        stream_smoke,
        "discover_markets",
        lambda *_args, **_kwargs: [
            DiscoveryCandidate(
                "US Tech 100", "US Tech 100", "CS.D.US. NASDAQ. MINI.IP", "TRADEABLE",
                "INDICES", "DFB", "CASH/ROLLING CFD", True, True, {},
            )
        ],
    )


def test_smoke_path_connects_and_receives_one_update_without_high_level_service(monkeypatch):
    fake_discovery(monkeypatch)
    created = []

    def factory(endpoint, adapter_set):
        client = FakeSDKClient(endpoint, adapter_set)
        created.append(client)
        return client

    result = run_stream_smoke(
        FakeRestClient(),
        1,
        client_factory=factory,
        subscription_factory=FakeSDKSubscription,
    )

    assert result.passed
    assert result.sdk_statuses == ["CONNECTING", "CONNECTED:STREAM-SENSING"]
    assert created[0].events[-1] == ("disconnect",)
    assert created[0].events.index(("connect",)) < created[0].events.index(("disconnect",))
    subscription = FakeSDKSubscription.created[-1]
    assert subscription.mode == "MERGE"
    assert subscription.items == ["PRICE:ACCOUNT:CS.D.US. NASDAQ. MINI.IP"]
    assert subscription.adapter == "Pricing"
    assert subscription.fields == ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG"]


def test_stream_smoke_command_connects_once_after_client_creation(monkeypatch):
    fake_discovery(monkeypatch)
    from ig_ai import cli, stream_smoke

    created = []

    def factory(endpoint, adapter_set):
        client = FakeSDKClient(endpoint, adapter_set)
        created.append(client)
        return client

    monkeypatch.setattr(stream_smoke, "_sdk", lambda: (factory, FakeSDKSubscription))
    monkeypatch.setattr(cli, "IGRestClient", lambda _settings: FakeRestClient())
    monkeypatch.setattr(cli.Settings, "from_env", lambda: object())
    monkeypatch.setattr(sys, "argv", ["ig-ai", "stream-smoke", "--duration", "1"])

    assert cli.main() == 0
    assert len(created) == 1
    assert [event for event in created[0].events if event == ("connect",)] == [("connect",)]


def test_smoke_fails_if_sdk_never_reaches_connected(monkeypatch):
    fake_discovery(monkeypatch)

    class NoConnect(FakeSDKClient):
        def connect(self):
            self.events.append(("connect",))

    result = run_stream_smoke(
        FakeRestClient(),
        0.05,
        client_factory=NoConnect,
        subscription_factory=FakeSDKSubscription,
    )

    assert not result.passed
    assert result.sdk_client_created
    assert result.connect_invoked
    assert not result.item_update_received


def test_smoke_output_does_not_include_credentials_or_prices():
    from ig_ai.stream_smoke import SmokeResult

    output = format_smoke_result(SmokeResult(sdk_statuses=["CONNECTING", "CONNECTED:STREAM-SENSING"]))
    assert "cst" not in output.lower()
    assert "token" not in output.lower()
    assert "100" not in output
