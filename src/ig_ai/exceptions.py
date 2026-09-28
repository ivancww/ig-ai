class IGError(Exception):
    """Base error for the application."""


class ConfigurationError(IGError):
    """Required configuration is missing or invalid."""


class IGHTTPError(IGError):
    """An IG API request failed."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        retryable: bool = False,
        provider_code: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        phase: str | None = None,
    ):
        super().__init__(f"IG HTTP {status}: {message}")
        self.status = status
        self.retryable = retryable
        self.provider_code = provider_code
        self.endpoint = endpoint
        self.method = method
        self.phase = phase

    def safe_diagnostic(self) -> str:
        """Return only the fields approved for runtime diagnostics."""
        fields = [
            f"status={self.status}",
            f"provider_code={self.provider_code or 'none'}",
            f"endpoint={self.endpoint or 'unknown'}",
            f"method={self.method or 'unknown'}",
            f"phase={self.phase or 'unknown'}",
        ]
        return "IG HTTP diagnostic: " + " ".join(fields)


class AuthenticationError(IGHTTPError):
    """IG rejected authentication or session credentials."""


class RateLimitError(IGHTTPError):
    """IG throttled a request."""


class MalformedResponseError(IGError):
    """IG returned a response that could not be interpreted."""
