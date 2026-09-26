import io
from urllib.error import HTTPError

import pytest

from ig_ai.config import Settings
from ig_ai.exceptions import AuthenticationError, MalformedResponseError, RateLimitError
from ig_ai.rest import IGRestClient


def settings():
    return Settings.from_env({"IG_API_KEY": "key", "IG_USERNAME": "user", "IG_PASSWORD": "pass"})


class Response:
    headers = {"CST": "cst", "X-SECURITY-TOKEN": "token"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return io.BytesIO(b'{"lightstreamerEndpoint":"wss://example"}').read()


def test_authentication_creates_private_session():
    client = IGRestClient(settings(), opener=lambda *_args, **_kwargs: Response())
    result = client.authenticate()
    assert result["lightstreamerEndpoint"].startswith("wss://")
    assert client.session and client.session.cst == "cst"


def test_unauthorized_clears_session():
    def opener(*_args, **_kwargs):
        raise HTTPError("https://example", 401, "no", {}, io.BytesIO())

    client = IGRestClient(settings(), opener=opener)
    with pytest.raises(AuthenticationError):
        client.authenticate()
    assert client.session is None


def test_malformed_json_is_reported():
    class BadResponse(Response):
        def read(self):
            return b"not-json"

    client = IGRestClient(settings(), opener=lambda *_args, **_kwargs: BadResponse())
    with pytest.raises(MalformedResponseError):
        client.authenticate()


def test_rate_limit_is_typed_and_does_not_leak_response_body():
    def opener(*_args, **_kwargs):
        raise HTTPError("https://example", 429, "secret", {}, io.BytesIO(b"token"))

    client = IGRestClient(settings(), opener=opener)
    with pytest.raises(RateLimitError) as error:
        client._request("GET", "/markets", retry=False)
    assert "token" not in str(error.value).lower()


@pytest.mark.parametrize("method,path", [("POST", "/positions"), ("PUT", "/markets/EPIC")])
def test_non_read_only_requests_are_rejected_before_network(method, path):
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    client = IGRestClient(settings(), opener=opener)
    with pytest.raises(ValueError, match="only market-data GET requests"):
        client._request(method, path)
    assert not called
