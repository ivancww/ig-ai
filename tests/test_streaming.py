from ig_ai.streaming import (
    IGStreamService,
    Subscription,
    WebSocketLightstreamerTransport,
    lightstreamer_password,
)


class FakeTransport:
    def __init__(self):
        self.connects = 0
        self.subscriptions = []
        self.closed = 0
        self.updates = [{"BID": "1"}, None, {"BID": "2"}]

    def connect(self, endpoint, username, password):
        self.connects += 1

    def send(self, command, params):
        self.subscriptions.append((command, params))

    def receive(self):
        return self.updates.pop(0) if self.updates else None

    def close(self):
        self.closed += 1


def test_reconnects_and_resubscribes():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", "secret", transport, reconnect_seconds=0)
    service.add_subscription(Subscription("id", "MARKET:EPIC"))
    received = []

    def receive(update):
        received.append(update)
        if update == {"BID": "2"}:
            service.stop()

    service.run(receive)
    assert transport.connects == 2
    assert len(transport.subscriptions) == 2
    assert received == [{"BID": "1"}, {"BID": "2"}]


def test_secure_lightstreamer_password_and_no_duplicate_subscriptions():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", lightstreamer_password("cst", "token"), transport, reconnect_seconds=0)
    subscription = Subscription("id", "MARKET:EPIC")
    service.add_subscription(subscription)
    service.add_subscription(subscription)
    assert len(service.subscriptions) == 1
    assert service.password == "CST-cst|X-SECURITY-TOKEN-token"


def test_lightstreamer_parser_carries_forward_partial_fields():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = []
    assert transport._parse("SUB|1|OK|MARKET:EPIC|BID|OFFER|UPDATE_TIME|MARKET_STATE")['type'] == "SUB"
    first = transport._parse("ITEM1|100|102|12:00:00|TRADEABLE")
    second = transport._parse("ITEM1||103||")
    assert first["BID"] == "100" and first["OFFER"] == "102"
    assert "BID" not in second and second["OFFER"] == "103"


def test_stale_detection_uses_last_update():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", "secret", transport)
    service.add_subscription(Subscription("id", "MARKET:EPIC"))
    service.stats.last_update["id"] = 10.0
    assert service.is_stale("id", now=101.0, threshold=90)
    assert not service.is_stale("missing", now=101.0)
