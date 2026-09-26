from ig_ai.streaming import IGStreamService, Subscription


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
