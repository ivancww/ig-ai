from urllib.parse import urlsplit

import pytest

from ig_ai.streaming import (
    IGStreamService,
    LightstreamerError,
    Subscription,
    WebSocketLightstreamerTransport,
    lightstreamer_password,
    lightstreamer_ws_endpoint,
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


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def recv(self):
        return self.incoming.pop(0) if self.incoming else None

    def close(self):
        self.closed = True


class FakeWebSocket:
    def __init__(self, socket):
        self.socket = socket
        self.kwargs = None

    def create_connection(self, endpoint, **kwargs):
        self.endpoint = endpoint
        self.kwargs = kwargs
        return self.socket


def test_reconnects_and_resubscribes():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", "secret", transport, reconnect_seconds=0)
    service.add_subscription(Subscription("id", "PRICE:ACCOUNT:EPIC"))
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
    service = IGStreamService(
        "endpoint", "user", lightstreamer_password("cst", "token"), transport, reconnect_seconds=0
    )
    subscription = Subscription("id", "PRICE:ACCOUNT:EPIC")
    service.add_subscription(subscription)
    service.add_subscription(subscription)
    assert len(service.subscriptions) == 1
    assert service.password == "CST-cst|XST-token"


def test_endpoint_uses_ws_scheme_and_documented_path_without_query():
    assert lightstreamer_ws_endpoint("https://stream.example/base?secret=hidden") == (
        "wss://stream.example/base/lightstreamer"
    )
    assert lightstreamer_ws_endpoint("http://stream.example/lightstreamer/") == (
        "ws://stream.example/lightstreamer"
    )


def test_websocket_session_flow_uses_protocol_and_subscribes_after_conok():
    socket = FakeSocket([
        "WSOK",
        "CONOK,session-id,50000,5000,*",
        "SUBOK,1,1,4",
        "U,1,PRICE:ACCOUNT:EPIC,100|101|123|TRADEABLE",
    ])
    websocket = FakeWebSocket(socket)
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._websocket = websocket
    transport._socket = None
    transport._field_names = []
    transport._endpoint = ""
    transport._session_id = None

    transport.connect("https://stream.example/", "ACCOUNT", "CST-cst|XST-token")
    assert websocket.endpoint == "wss://stream.example/lightstreamer"
    assert websocket.kwargs["subprotocols"] == ["TLCP-2.4.0.lightstreamer.com"]
    assert socket.sent[0] == "wsok"
    assert socket.sent[1].startswith("create_session\r\n")
    assert "LS_user=ACCOUNT" in socket.sent[1]
    assert "LS_password=CST-cst%7CXST-token" in socket.sent[1]
    assert "LS_adapter_set" not in socket.sent[1]

    transport.send("subscribe", {"LS_subId": "1", "item": "PRICE:ACCOUNT:EPIC", "fields": "BIDPRICE1,ASKPRICE1,TIMESTAMP,DLG_FLAG", "adapter": "Pricing"})
    assert socket.sent[2].startswith("control\r\n")
    assert "LS_data_adapter=Pricing" in socket.sent[2]
    assert "LS_session=session-id" in socket.sent[2]
    assert transport.receive() == {"type": "SUB", "subscription_id": "1"}
    assert transport.receive()["BIDPRICE1"] == "100"


def test_handshake_status_is_safe_and_structurally_invalid_not_retried():
    class BadStatus(Exception):
        status_code = 403

    class BadWebSocket:
        def create_connection(self, *_args, **_kwargs):
            raise BadStatus("secret-token")

    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._websocket = BadWebSocket()
    transport._socket = None
    transport._field_names = []
    transport._endpoint = ""
    transport._session_id = None
    with pytest.raises(LightstreamerError) as error:
        transport.connect("https://stream.example/lightstreamer?secret=hidden", "user", "secret")
    diagnostic = error.value.safe_diagnostic()
    assert "status=403" in diagnostic
    assert "phase=websocket_handshake" in diagnostic
    assert "stream.example/lightstreamer" in diagnostic
    assert "secret" not in diagnostic
    assert error.value.retryable is False

    class FailingTransport:
        def connect(self, *_args):
            raise error.value

        def close(self):
            pass

    service = IGStreamService("endpoint", "user", "secret", FailingTransport(), reconnect_seconds=0)
    service.run(lambda _: None)
    assert service.stats.reconnect_count == 0
    assert service.stats.connection_state == "DISCONNECTED"
    assert len(service.stats.warnings) == 1
    assert "status=403" in service.stats.warnings[0]
    assert "secret" not in service.stats.warnings[0]


def test_lightstreamer_parser_carries_forward_partial_fields():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG"]
    first = transport._parse("U,1,PRICE:EPIC,100|102|123|TRADEABLE")
    second = transport._parse("U,1,PRICE:EPIC,100|^1|124|$")
    assert first["BIDPRICE1"] == "100" and first["ASKPRICE1"] == "102"
    assert second["BIDPRICE1"] == "100" and second["TIMESTAMP"] == "124"


def test_stale_detection_uses_last_update():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", "secret", transport)
    service.add_subscription(Subscription("id", "PRICE:ACCOUNT:EPIC"))
    service.stats.last_update["id"] = 10.0
    assert service.is_stale("id", now=101.0, threshold=90)
    assert not service.is_stale("missing", now=101.0)


def test_endpoint_diagnostic_has_no_query_or_credentials():
    parsed = urlsplit(lightstreamer_ws_endpoint("https://host.example/path?CST=secret"))
    assert parsed.query == ""
