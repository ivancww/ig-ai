from __future__ import annotations

import sys
import threading
import types

from ig_ai.streaming import (
    REQUIRED_FIELDS,
    IGStreamService,
    OfficialLightstreamerTransport,
    StreamDiagnostics,
    StreamStats,
    Subscription,
    configure_lightstreamer_connection,
    lightstreamer_password,
)


class FakeUpdate:
    def __init__(self, item: str, values: dict[str, str]):
        self.item = item
        self.values = values

    def getValue(self, field: str):
        return self.values.get(field)

    def getItemName(self):
        return self.item


class FakeSubscription:
    instances = []

    def __init__(self, mode, items, fields):
        self.mode, self.items, self.fields = mode, items, fields
        self.adapter = None
        self.listener = None
        FakeSubscription.instances.append(self)

    def setDataAdapter(self, adapter):
        self.adapter = adapter

    def addListener(self, listener):
        self.listener = listener


class FakeClient:
    instances = []

    def __init__(self, endpoint, adapter_set):
        self.endpoint, self.adapter_set = endpoint, adapter_set
        self.connectionDetails = FakeConnectionDetails(self)
        self.listener = None
        self.subscriptions = []
        self.events = []
        FakeClient.instances.append(self)

    def addListener(self, listener):
        self.events.append("addListener")
        self.listener = listener

    def connect(self):
        self.events.append("connect")
        self.listener.onStatusChange("CONNECTED:STREAM-SENSING")

    def subscribe(self, subscription):
        self.subscriptions.append(subscription)
        subscription.listener.onSubscription()

    def disconnect(self):
        self.listener.onStatusChange("DISCONNECTED")


class FakeConnectionDetails:
    def __init__(self, client):
        self.client = client
        self.user = self.password = None

    def setUser(self, user):
        self.client.events.append("connectionDetails.setUser")
        self.user = user

    def setPassword(self, password):
        self.client.events.append("connectionDetails.setPassword")
        self.password = password


class FakeListener:
    pass


def install_sdk(monkeypatch):
    package = types.ModuleType("lightstreamer")
    client = types.ModuleType("lightstreamer.client")
    client.LightstreamerClient = FakeClient
    client.Subscription = FakeSubscription
    client.SubscriptionListener = FakeListener
    package.client = client
    monkeypatch.setitem(sys.modules, "lightstreamer", package)
    monkeypatch.setitem(sys.modules, "lightstreamer.client", client)
    FakeClient.instances.clear()
    FakeSubscription.instances.clear()


def test_password_and_sdk_configuration_are_safe(monkeypatch):
    install_sdk(monkeypatch)
    transport = OfficialLightstreamerTransport()
    transport.connect("https://stream.example/lightstreamer?secret=hidden", "ACCOUNT", "CST-secret|XST-token")
    client = FakeClient.instances[0]
    assert client.endpoint == "https://stream.example/lightstreamer?secret=hidden"
    assert client.adapter_set == "DEFAULT"
    assert client.connectionDetails.user == "ACCOUNT"
    assert client.connectionDetails.password == "CST-secret|XST-token"
    assert lightstreamer_password("cst", "token") == "CST-cst|XST-token"
    assert "CST-secret" not in transport.diagnostics.sdk_statuses


def test_sdk_connection_waits_for_async_status_and_preserves_configuration_order(monkeypatch):
    install_sdk(monkeypatch)

    class AsyncClient(FakeClient):
        def connect(self):
            self.events.append("connect")
            threading.Timer(
                0.01, lambda: self.listener.onStatusChange("CONNECTED:STREAM-SENSING")
            ).start()

    transport = OfficialLightstreamerTransport(client_factory=AsyncClient, connection_timeout=1)
    transport.connect(
        "https://stream.example/lightstreamer",
        "ACTIVE-ACCOUNT",
        "CST-secret|XST-token",
    )

    client = AsyncClient.instances[0]
    assert client.events == [
        "connectionDetails.setUser",
        "connectionDetails.setPassword",
        "addListener",
        "connect",
    ]
    assert transport.diagnostics.sdk_client_created
    assert transport.diagnostics.connect_invoked
    assert transport.diagnostics.connection_verified
    assert transport.diagnostics.connection_wait_seconds >= 0.01
    assert transport.diagnostics.client_lifecycle_state == "CONNECTED"
    assert transport.diagnostics.sdk_statuses == ["CONNECTED:STREAM-SENSING"]


def test_credentials_require_sdk_connection_details_interface():
    class Client:
        def __init__(self):
            self.connectionDetails = Details()

    class Details:
        def __init__(self):
            self.values = []

        def setUser(self, value):
            self.values.append(("user", value))

        def setPassword(self, value):
            self.values.append(("password", value))

    client = Client()
    configure_lightstreamer_connection(client, "ACCOUNT", "CST-cst|XST-token")
    assert client.connectionDetails.values == [
        ("user", "ACCOUNT"),
        ("password", "CST-cst|XST-token"),
    ]


def test_sdk_server_error_is_captured_without_credentials(monkeypatch):
    install_sdk(monkeypatch)

    class ErrorClient(FakeClient):
        def connect(self):
            self.events.append("connect")
            self.listener.onStatusChange("CONNECTING")
            self.listener.onStatusChange("DISCONNECTED")
            self.listener.onServerError(1, "bad CST-secret|XST-token password")

    transport = OfficialLightstreamerTransport(client_factory=ErrorClient, connection_timeout=1)
    try:
        transport.connect("https://stream.example/lightstreamer", "ACCOUNT", "CST-secret|XST-token")
    except ConnectionError:
        pass
    else:
        raise AssertionError("connection should not be verified")

    assert transport.diagnostics.server_error_code == 1
    assert "CST-secret" not in transport.diagnostics.server_error_message
    assert "XST-token" not in transport.diagnostics.server_error_message
    assert transport.diagnostics.connection_verified is False


def test_merge_price_subscription_pricing_adapter_and_required_fields(monkeypatch):
    install_sdk(monkeypatch)
    transport = OfficialLightstreamerTransport()
    transport.connect("https://stream.example", "ACCOUNT", "password")
    transport.send("subscribe", {"LS_subId": "1", "item": "PRICE:ACCOUNT:EPIC", "fields": ",".join(REQUIRED_FIELDS), "adapter": "Pricing"})
    subscription = FakeSubscription.instances[0]
    assert subscription.mode == "MERGE"
    assert subscription.items == ["PRICE:ACCOUNT:EPIC"]
    assert subscription.fields == list(REQUIRED_FIELDS)
    assert subscription.adapter == "Pricing"
    assert transport.diagnostics.subscriptions_accepted == {"1"}


def test_item_update_uses_sdk_merge_state_and_reaches_service(monkeypatch):
    install_sdk(monkeypatch)
    transport = OfficialLightstreamerTransport()
    service = IGStreamService("https://stream.example", "ACCOUNT", "password", transport)
    service.add_subscription(Subscription("EPIC", "PRICE:ACCOUNT:EPIC"))
    received = []

    def on_update(update):
        received.append(update)
        service.stop()

    transport.connect("https://stream.example", "ACCOUNT", "password")
    transport.send("subscribe", {"LS_subId": "1", "item": "PRICE:ACCOUNT:EPIC", "fields": ",".join(REQUIRED_FIELDS), "adapter": "Pricing"})
    listener = FakeSubscription.instances[0].listener
    listener.onItemUpdate(FakeUpdate("PRICE:ACCOUNT:EPIC", {"BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000", "DLG_FLAG": "TRADEABLE"}))
    service._stop.clear()
    service.run(on_update)
    assert received[0]["BIDPRICE1"] == "100"
    assert received[0]["ASKPRICE1"] == "102"
    assert received[0]["TIMESTAMP"] == "1760000000000"
    assert received[0]["DLG_FLAG"] == "TRADEABLE"
    assert service.stats.updates_received["EPIC"] == 1
    assert transport.diagnostics.first_updates_received == {"1"}


def test_multiple_markets_are_isolated_and_sdk_diagnostics_are_safe():
    class FakeTransport:
        def __init__(self):
            self.diagnostics = StreamDiagnostics()
            self.updates = [
                {"subscription_id": "1", "BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000"},
                {"subscription_id": "2", "BIDPRICE1": "200", "ASKPRICE1": "202", "TIMESTAMP": "1760000001000"},
            ]

        def connect(self, endpoint, username, password):
            assert endpoint == "endpoint"
            assert username == "ACCOUNT"
            assert password == "secret"

        def send(self, command, params):
            pass

        def receive(self):
            return self.updates.pop(0) if self.updates else None

        def close(self):
            pass

    transport = FakeTransport()
    service = IGStreamService("endpoint", "ACCOUNT", "secret", transport)
    service.add_subscription(Subscription("ONE", "PRICE:ACCOUNT:ONE"))
    service.add_subscription(Subscription("TWO", "PRICE:ACCOUNT:TWO"))
    seen = []
    service.run(lambda update: (seen.append(update), service.stop()) if len(seen) == 1 else seen.append(update))
    assert {update["instrument_id"] for update in seen} == {"ONE", "TWO"}


def test_three_market_validation_requires_every_subscription_and_item_update():
    stats = StreamStats()
    stats.diagnostics.connection_verified = True
    stats.diagnostics.subscriptions_accepted = {"1", "2", "3"}
    stats.updates_received = {"US": 1, "JP": 1, "HK": 0}

    assert not stats.live_validation_passed(("US", "JP", "HK"))
    stats.updates_received["HK"] = 1
    assert stats.live_validation_passed(("US", "JP", "HK"))


def test_three_market_sdk_subscriptions_keep_price_schema_and_pricing_adapter(monkeypatch):
    install_sdk(monkeypatch)
    transport = OfficialLightstreamerTransport()
    transport.connect("https://stream.example", "ACCOUNT", "password")
    epics = ("US-EPIC", "JP-EPIC", "HK-EPIC")
    for index, epic in enumerate(epics, 1):
        transport.send(
            "subscribe",
            {
                "LS_subId": str(index),
                "item": f"PRICE:ACCOUNT:{epic}",
                "fields": ",".join(REQUIRED_FIELDS),
                "adapter": "Pricing",
            },
        )

    assert len(FakeSubscription.instances) == 3
    assert {subscription.items[0] for subscription in FakeSubscription.instances} == {
        f"PRICE:ACCOUNT:{epic}" for epic in epics
    }
    assert all(subscription.adapter == "Pricing" for subscription in FakeSubscription.instances)
    assert transport.diagnostics.subscriptions_accepted == {"1", "2", "3"}


def test_disconnect_state_is_reported_without_custom_reconnect():
    class Disconnected:
        diagnostics = StreamDiagnostics()
        def connect(self, *_):
            raise ConnectionError("closed")
        def send(self, *_):
            pass
        def receive(self):
            return None
        def close(self):
            pass

    service = IGStreamService("endpoint", "user", "secret", Disconnected())
    service.run(lambda _: None)
    assert service.stats.connection_state == "DISCONNECTED"
    assert service.stats.reconnect_count == 0
    assert service.stats.diagnostics.unexpected_disconnect


def test_official_sdk_recovery_is_observed_without_duplicate_application_subscription(monkeypatch):
    install_sdk(monkeypatch)

    class RecoveryClient(FakeClient):
        def subscribe(self, subscription):
            self.subscriptions.append(subscription)
            subscription.listener.onSubscription()
            self.listener.onStatusChange("DISCONNECTED")
            self.listener.onStatusChange("CONNECTING")
            self.listener.onStatusChange("CONNECTED:STREAM-SENSING")
            subscription.listener.onItemUpdate(
                FakeUpdate(
                    "PRICE:ACCOUNT:EPIC",
                    {"BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000", "DLG_FLAG": "DEAL"},
                )
            )

    transport = OfficialLightstreamerTransport(client_factory=RecoveryClient)
    service = IGStreamService("https://stream.example", "ACCOUNT", "password", transport)
    service.add_subscription(Subscription("EPIC", "PRICE:ACCOUNT:EPIC"))
    received = []

    def on_update(update):
        received.append(update)
        service.stop()

    service.run(on_update)
    assert received[0]["instrument_id"] == "EPIC"
    assert service.reconnect_status == "VERIFIED"
    assert service.stats.reconnect_count == 1
    assert len(RecoveryClient.instances[0].subscriptions) == 1
    assert transport.diagnostics.subscriptions_accepted == {"1"}
