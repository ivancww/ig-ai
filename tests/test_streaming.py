from __future__ import annotations

import sys
import types

from ig_ai.streaming import (
    REQUIRED_FIELDS,
    IGStreamService,
    OfficialLightstreamerTransport,
    StreamDiagnostics,
    Subscription,
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
        self.user = self.password = None
        self.listener = None
        self.subscriptions = []
        FakeClient.instances.append(self)

    def setUser(self, user):
        self.user = user

    def setPassword(self, password):
        self.password = password

    def addListener(self, listener):
        self.listener = listener

    def connect(self):
        self.listener.onStatusChange("CONNECTED:STREAM-SENSING")

    def subscribe(self, subscription):
        self.subscriptions.append(subscription)
        subscription.listener.onSubscription()

    def disconnect(self):
        self.listener.onStatusChange("DISCONNECTED")


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
    assert client.user == "ACCOUNT"
    assert client.password == "CST-secret|XST-token"
    assert lightstreamer_password("cst", "token") == "CST-cst|XST-token"
    assert "CST-secret" not in transport.diagnostics.sdk_statuses


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
