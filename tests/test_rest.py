import io
from urllib.error import HTTPError

import pytest

from ig_ai.config import Settings
from ig_ai.exceptions import (
    AuthenticationError,
    IGHTTPError,
    MalformedResponseError,
    RateLimitError,
)
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
        return io.BytesIO(b'{"lightstreamerEndpoint":"wss://example","currentAccountId":"ABC123"}').read()


def test_authentication_creates_private_session():
    client = IGRestClient(settings(), opener=lambda *_args, **_kwargs: Response())
    result = client.authenticate()
    assert result["lightstreamerEndpoint"].startswith("wss://")
    assert client.session and client.session.cst == "cst"
    assert client.session.account_id == "ABC123"


def test_unauthorized_clears_session():
    def opener(*_args, **_kwargs):
        raise HTTPError(
            "https://example", 401, "no",
            {"Authorization": "Bearer authorization-secret", "CST": "cst-secret"},
            io.BytesIO(
                b'{"errorCode":"error.public-api.failure.invalid.client.token",'
                b'"errorMessage":"password=pass-secret"}'
            ),
        )

    client = IGRestClient(settings(), opener=opener)
    with pytest.raises(AuthenticationError) as error:
        client.authenticate()
    assert client.session is None
    diagnostic = error.value.safe_diagnostic()
    assert (
        diagnostic
        == "IG HTTP diagnostic: status=401 "
        "provider_code=error.public-api.failure.invalid.client.token "
        "endpoint=/session method=POST phase=authentication"
    )
    assert "pass-secret" not in diagnostic
    assert "authorization-secret" not in diagnostic
    assert "CST" not in diagnostic


@pytest.mark.parametrize(
    ("status", "expected_type", "path", "method", "phase"),
    [
        (403, IGHTTPError, "/markets", "GET", "search"),
        (429, RateLimitError, "/markets", "GET", "search"),
        (500, IGHTTPError, "/markets/EPIC", "GET", "instrument_details"),
        (503, IGHTTPError, "/markets/EPIC", "GET", "instrument_details"),
    ],
)
def test_http_diagnostics_are_safe_for_provider_failures(
    status, expected_type, path, method, phase
):
    secrets = b"api-key-secret username-secret password-secret cst-secret token-secret"

    def opener(*_args, **_kwargs):
        raise HTTPError(
            "https://example/gateway/deal?secret=query-secret",
            status,
            "ignored reason",
            {
                "Authorization": "Bearer authorization-secret",
                "X-SECURITY-TOKEN": "security-token-secret",
            },
            io.BytesIO(
                b'{"errorCode":"error.public-api.failure.provider-code",'
                b'"errorMessage":"' + secrets + b'"}'
            ),
        )

    client = IGRestClient(settings(), opener=opener)
    with pytest.raises(expected_type) as error:
        client._request(method, path, retry=False)

    diagnostic = error.value.safe_diagnostic()
    assert "status=" + str(status) in diagnostic
    assert "provider_code=error.public-api.failure.provider-code" in diagnostic
    assert f"endpoint={path}" in diagnostic
    assert f"method={method}" in diagnostic
    assert f"phase={phase}" in diagnostic
    for secret in (
        b"api-key-secret", b"username-secret", b"password-secret", b"cst-secret",
        b"token-secret", b"authorization-secret", b"security-token-secret", b"query-secret",
    ):
        assert secret.decode() not in diagnostic
    assert "https://" not in diagnostic


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
