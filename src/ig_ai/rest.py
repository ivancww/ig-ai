from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings
from .exceptions import AuthenticationError, IGHTTPError, MalformedResponseError, RateLimitError

log = logging.getLogger(__name__)


@dataclass
class IGSession:
    cst: str
    security_token: str
    lightstreamer_endpoint: str | None = None
    account_id: str | None = None


class IGRestClient:
    """Dependency-free, read-only-capable IG REST client."""

    _READ_ONLY_GET_PREFIXES = ("/markets", "/prices/")

    def __init__(self, settings: Settings, opener=urlopen):
        self.settings = settings
        self._opener = opener
        self.session: IGSession | None = None

    def authenticate(self) -> dict[str, Any]:
        payload = {
            "identifier": self.settings.username,
            "password": self.settings.password,
            "accountType": self.settings.account_type,
        }
        data, headers = self._request("POST", "/session", payload=payload, version="3", retry=False)
        cst = headers.get("CST") or headers.get("cst")
        token = headers.get("X-SECURITY-TOKEN") or headers.get("x-security-token")
        if not cst or not token:
            raise MalformedResponseError(
                "IG authentication response did not include session headers"
            )
        self.session = IGSession(
            cst,
            token,
            data.get("lightstreamerEndpoint"),
            data.get("currentAccountId") or data.get("accountId"),
        )
        return data

    def _headers(self, version: str) -> dict[str, str]:
        result = {
            "Accept": f"application/json;version={version}",
            "Content-Type": "application/json",
            "X-IG-API-KEY": self.settings.api_key,
        }
        if self.session:
            result.update(
                {"CST": self.session.cst, "X-SECURITY-TOKEN": self.session.security_token}
            )
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
        version: str = "1",
        retry: bool = True,
        phase: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        read_only_get = method == "GET" and any(
            path == prefix or path.startswith(prefix)
            for prefix in self._READ_ONLY_GET_PREFIXES
        )
        authentication_post = method == "POST" and path == "/session"
        if not (read_only_get or authentication_post):
            raise ValueError(
                "IG client permits only market-data GET requests and POST /session authentication"
            )
        url = f"{self.settings.api_base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        attempts = 3 if retry and method == "GET" else 1
        for attempt in range(attempts):
            request = Request(
                url,
                method=method,
                headers=self._headers(version),
                data=json.dumps(payload).encode() if payload is not None else None,
            )
            try:
                with self._opener(
                    request, timeout=self.settings.request_timeout_seconds
                ) as response:
                    raw = response.read()
                    try:
                        decoded = json.loads(raw) if raw else {}
                    except json.JSONDecodeError as exc:
                        raise MalformedResponseError("IG response was not valid JSON") from exc
                    if not isinstance(decoded, dict):
                        raise MalformedResponseError("IG response was not a JSON object")
                    return decoded, dict(response.headers)
            except HTTPError as exc:
                provider_code = self._safe_provider_code(exc)
                diagnostic = {
                    "provider_code": provider_code,
                    "endpoint": path,
                    "method": method,
                    "phase": phase or self._phase_for(path),
                }
                if exc.code == 401:
                    self.session = None
                    raise AuthenticationError(
                        exc.code, "authentication rejected", **diagnostic
                    ) from exc
                if exc.code == 429:
                    if attempt + 1 < attempts:
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise RateLimitError(
                        exc.code, "rate limit exceeded", retryable=True, **diagnostic
                    ) from exc
                retryable = exc.code == 429 or exc.code >= 500
                if retryable and attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise IGHTTPError(
                    exc.code, "request failed", retryable=retryable, **diagnostic
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise IGHTTPError(
                    0,
                    "network request failed",
                    retryable=True,
                    endpoint=path,
                    method=method,
                    phase=phase or self._phase_for(path),
                ) from exc
        raise IGHTTPError(
            0,
            "request failed",
            endpoint=path,
            method=method,
            phase=phase or self._phase_for(path),
        )

    @staticmethod
    def _phase_for(path: str) -> str:
        if path == "/session":
            return "authentication"
        if path == "/markets":
            return "search"
        return "instrument_details"

    @staticmethod
    def _safe_provider_code(error: HTTPError) -> str | None:
        """Parse only the explicitly approved provider error-code field."""
        try:
            raw = error.read()
            decoded = json.loads(raw) if raw else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        provider_code = decoded.get("errorCode")
        return provider_code if isinstance(provider_code, str) and provider_code else None

    def ensure_session(self) -> IGSession:
        if self.session is None:
            self.authenticate()
        assert self.session is not None
        return self.session

    def refresh_session(self) -> IGSession:
        """Explicitly replace an expired session without exposing its tokens."""
        self.session = None
        return self.ensure_session()

    def search_markets(self, search_term: str) -> list[dict[str, Any]]:
        self.ensure_session()
        data, _ = self._request(
            "GET", "/markets", params={"searchTerm": search_term}, version="1", phase="search"
        )
        markets = data.get("markets", [])
        if not isinstance(markets, list):
            raise MalformedResponseError("IG market search returned invalid markets")
        return markets

    def market_details(self, epic: str) -> dict[str, Any]:
        self.ensure_session()
        data, _ = self._request(
            "GET", f"/markets/{epic}", version="3", phase="instrument_details"
        )
        return data

    def prices(self, epic: str, resolution: str = "MINUTE", **params: str) -> list[dict[str, Any]]:
        self.ensure_session()
        data, _ = self._request("GET", f"/prices/{epic}/{resolution}", params=params, version="3")
        prices = data.get("prices", [])
        if not isinstance(prices, list):
            raise MalformedResponseError("IG prices response returned invalid prices")
        return prices
