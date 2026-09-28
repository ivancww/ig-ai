from urllib.parse import urlsplit

import pytest

from ig_ai.streaming import (
    IGStreamService,
    LightstreamerError,
    StreamDiagnostics,
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
    # TLCP does not require LS_reqId on ordinary create_session requests;
    # standalone control requests do require it.
    assert "LS_reqId" not in socket.sent[1]

    transport.send("subscribe", {"LS_subId": "1", "item": "PRICE:ACCOUNT:EPIC", "fields": "BIDPRICE1,ASKPRICE1,TIMESTAMP,DLG_FLAG", "adapter": "Pricing"})
    assert socket.sent[2].startswith("control\r\n")
    assert "LS_reqId=1" in socket.sent[2]
    assert "LS_data_adapter=Pricing" in socket.sent[2]
    assert "LS_session=session-id" in socket.sent[2]
    assert transport.receive() == {"type": "SUB", "subscription_id": "1"}
    assert transport.receive()["BIDPRICE1"] == "100"
    assert transport.diagnostics.websocket_handshake_accepted
    assert transport.diagnostics.wsok_received
    assert transport.diagnostics.create_session_sent
    assert transport.diagnostics.conok_received
    assert transport.diagnostics.session_id_established
    assert transport.diagnostics.control_sent
    assert transport.diagnostics.subscriptions_accepted == {"1"}
    assert transport.diagnostics.u_messages_received == {"1"}
    assert transport.diagnostics.server_messages == ["SUBOK"]


def test_control_request_ids_are_sequential_and_reset_for_new_session():
    first_socket = FakeSocket(["WSOK", "CONOK,first-session,50000,5000,*"])
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._websocket = FakeWebSocket(first_socket)
    transport._socket = None
    transport._field_names = []
    transport._endpoint = ""
    transport._session_id = None
    transport._next_request_id = 999

    transport.connect("https://stream.example/", "ACCOUNT", "password")
    for subscription_id in ("1", "2"):
        transport.send(
            "subscribe",
            {
                "LS_subId": subscription_id,
                "item": f"PRICE:ACCOUNT:EPIC{subscription_id}",
                "fields": "BIDPRICE1,ASKPRICE1,TIMESTAMP,DLG_FLAG",
            },
        )
    assert "LS_reqId=1" in first_socket.sent[2]
    assert "LS_reqId=2" in first_socket.sent[3]
    assert len({first_socket.sent[2], first_socket.sent[3]}) == 2

    second_socket = FakeSocket(["WSOK", "CONOK,second-session,50000,5000,*"])
    transport._websocket = FakeWebSocket(second_socket)
    transport.close()
    transport.connect("https://stream.example/", "ACCOUNT", "password")
    transport.send(
        "subscribe",
        {
            "LS_subId": "1",
            "item": "PRICE:ACCOUNT:EPIC1",
            "fields": "BIDPRICE1,ASKPRICE1,TIMESTAMP,DLG_FLAG",
        },
    )
    assert "LS_reqId=1" in second_socket.sent[2]
    assert "LS_session=second-session" in second_socket.sent[2]


def test_subscription_requires_conok_session_establishment():
    socket = FakeSocket(["WSOK", "PROBE"])
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._websocket = FakeWebSocket(socket)
    transport._socket = None
    transport._field_names = []
    transport._endpoint = ""
    transport._session_id = None
    transport._next_request_id = 1

    with pytest.raises(LightstreamerError):
        transport.connect("https://stream.example/", "ACCOUNT", "password")
    with pytest.raises(ConnectionError, match="session is not established"):
        transport.send(
            "subscribe",
            {"LS_subId": "1", "item": "PRICE:ACCOUNT:EPIC", "fields": "BIDPRICE1"},
        )


def test_service_passes_subscription_data_adapter():
    transport = FakeTransport()
    service = IGStreamService("endpoint", "user", "secret", transport, reconnect_seconds=0)
    service.add_subscription(Subscription("id", "PRICE:ACCOUNT:EPIC", data_adapter="Pricing"))

    service.run(lambda _: service.stop())
    assert transport.subscriptions[0][1]["adapter"] == "Pricing"


def test_service_correlates_real_numeric_item_by_subscription_id():
    class UTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.updates = [
                {
                    "item": "1",
                    "subscription_id": "1",
                    "BIDPRICE1": "100",
                    "ASKPRICE1": "102",
                    "TIMESTAMP": "1760000000000",
                }
            ]

    transport = UTransport()
    service = IGStreamService("endpoint", "user", "secret", transport, reconnect_seconds=0)
    service.add_subscription(Subscription("EPIC", "PRICE:ACCOUNT:EPIC"))
    received = []

    def receive(update):
        received.append(update)
        service.stop()

    service.run(receive)
    assert received[0]["instrument_id"] == "EPIC"
    assert received[0]["epic"] == "EPIC"
    assert service.stats.diagnostics.first_updates_received == {"1"}


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
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.subscription_requests_sent["1"] = "PRICE:EPIC"
    first = transport._parse("U,1,PRICE:EPIC,100|102|123|TRADEABLE")
    second = transport._parse("U,1,PRICE:EPIC,100|^1|124|$")
    assert first["BIDPRICE1"] == "100" and first["ASKPRICE1"] == "102"
    assert second["BIDPRICE1"] == "100" and second["TIMESTAMP"] == "124"


def test_realistic_u_decoder_resolves_item_index_delta_and_null_empty_values():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP", "DLG_FLAG"]
    transport._secrets = ()
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.subscription_requests_sent["1"] = "PRICE:ACCOUNT:EPIC"

    first = transport._parse("U,1,1,100|102|1760000000000|DEAL")
    second = transport._parse("U,1,1,^2|1770000000000|$")
    third = transport._parse("U,1,1,#|102|^1|TRADEABLE")

    assert first["BIDPRICE1"] == "100" and first["ASKPRICE1"] == "102"
    assert second["BIDPRICE1"] == "100" and second["ASKPRICE1"] == "102"
    assert second["DLG_FLAG"] == ""
    assert third["BIDPRICE1"] is None and third["ASKPRICE1"] == "102"
    assert transport.diagnostics.u_messages_received == {"1"}
    diagnostic = transport.diagnostics.safe_update_diagnostics[-1]
    assert diagnostic["subscription_id"] == "1"
    assert diagnostic["bid_decoded"] == "False"
    assert "100" not in str(diagnostic)


def test_decoder_keeps_subscription_and_item_state_isolated():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = []
    transport._field_names_by_subscription = {"1": ("BID", "OFFER"), "2": ("BID", "OFFER")}
    transport._field_state = {}
    transport._secrets = ()
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.subscription_requests_sent.update({"1": "PRICE:EPIC1", "2": "PRICE:EPIC2"})

    first = transport._parse("U,1,1,10|12")
    second = transport._parse("U,2,1,20|22")
    delta = transport._parse("U,1,1,|13")
    wrong_item = transport._parse("U,1,2,99|100")

    assert first["BID"] == "10" and first["OFFER"] == "12"
    assert second["BID"] == "20" and second["OFFER"] == "22"
    assert delta["BID"] == "10" and delta["OFFER"] == "13"
    assert wrong_item["BID"] == "99"
    assert transport._field_state[("1", "1")]["BID"] == "10"
    assert transport._field_state[("1", "2")]["BID"] == "99"


def test_lightstreamer_parser_tracks_keepalives_and_rejects_protocol_errors_safely():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = ["BIDPRICE1"]
    transport._secrets = ("CST-secret|XST-token",)
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.subscription_requests_sent["1"] = "PRICE:EPIC"
    assert transport._parse("LOOP,123") == {"type": "LOOP"}
    assert transport._parse("PROG,456") == {"type": "PROG"}
    assert transport.diagnostics.server_messages == ["LOOP", "PROG"]
    with pytest.raises(LightstreamerError) as error:
        transport._parse("REQERR,1,LS_reqId,invalid CST-secret|XST-token")
    assert error.value.provider_error == "LS_reqId"
    assert "CST-secret" not in error.value.safe_diagnostic()
    assert transport.diagnostics.protocol_errors == ["REQERR"]


def test_lightstreamer_parser_tracks_control_lifecycle_without_emitting_control_frames():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = ["BIDPRICE1"]
    transport._secrets = ()
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.control_requests["1"] = "1"
    transport.diagnostics.subscription_requests_sent["1"] = "PRICE:EPIC"

    assert transport._parse("REQOK,1") == {"type": "REQOK", "request_id": "1", "subscription_id": "1"}
    assert transport._parse("SUBCMD,1,1,2,1,2") == {"type": "SUBCMD", "subscription_id": "1"}
    assert transport._parse("SUBOK,1,1,1") == {"type": "SUB", "subscription_id": "1"}
    assert transport._parse("U,1,PRICE:EPIC,100") == {
        "item": "PRICE:EPIC",
        "subscription_id": "1",
        "BIDPRICE1": "100",
    }
    assert transport.diagnostics.server_messages == ["REQOK", "SUBCMD", "SUBOK"]
    assert transport.diagnostics.u_messages_received == {"1"}
    assert transport.diagnostics.reqok_request_ids == {"1"}
    assert transport.diagnostics.subcmd_subscription_ids == {"1"}
    assert transport.diagnostics.subok_subscription_ids == {"1"}
    assert transport.diagnostics.subscription_states["1"] == "SUBSCRIPTION_ESTABLISHED"


def test_control_and_subscription_correlations_do_not_false_accept():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = ["BIDPRICE1"]
    transport._secrets = ()
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.control_requests = {"11": "1", "12": "2", "13": "3"}
    transport.diagnostics.subscription_requests_sent = {
        "1": "PRICE:EPIC1", "2": "PRICE:EPIC2", "3": "PRICE:EPIC3"
    }

    assert transport._parse("REQOK,12")["subscription_id"] == "2"
    with pytest.raises(LightstreamerError):
        transport._parse("REQERR,13,68,safe failure")
    assert transport.diagnostics.reqok_request_ids == {"12"}
    assert transport.diagnostics.reqerr_request_ids == {"13"}
    assert transport.diagnostics.subscription_states["2"] == "CONTROL_ACCEPTED"
    assert "3" not in transport.diagnostics.subscriptions_accepted

    transport._parse("SUBOK,2,1,1")
    transport._parse("SUBCMD,1,1,2,1,2")
    transport._parse("U,2,PRICE:EPIC2,100")
    transport._parse("U,99,UNKNOWN,999")
    assert transport.diagnostics.subok_subscription_ids == {"2"}
    assert transport.diagnostics.subcmd_subscription_ids == {"1"}
    assert transport.diagnostics.u_messages_received == {"2"}
    assert "99" not in transport.diagnostics.first_updates_received
    assert transport.diagnostics.subscription_states["2"] == "SUBSCRIPTION_ESTABLISHED"


def test_reqerr_correlation_redacts_error_content():
    transport = object.__new__(WebSocketLightstreamerTransport)
    transport._field_names = []
    transport._secrets = ("CST-secret|XST-token",)
    transport.diagnostics = StreamDiagnostics()
    transport.diagnostics.control_requests["7"] = "1"
    transport.diagnostics.subscription_requests_sent["1"] = "PRICE:EPIC"
    with pytest.raises(LightstreamerError) as error:
        transport._parse("REQERR,7,68,invalid CST-secret|XST-token")
    assert transport.diagnostics.reqerr_request_ids == {"7"}
    assert "CST-secret" not in error.value.safe_diagnostic()


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
