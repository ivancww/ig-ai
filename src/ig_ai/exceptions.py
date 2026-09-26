class IGError(Exception):
    """Base error for the application."""


class ConfigurationError(IGError):
    """Required configuration is missing or invalid."""


class IGHTTPError(IGError):
    """An IG API request failed."""

    def __init__(self, status: int, message: str, *, retryable: bool = False):
        super().__init__(f"IG HTTP {status}: {message}")
        self.status = status
        self.retryable = retryable


class AuthenticationError(IGHTTPError):
    """IG rejected authentication or session credentials."""


class MalformedResponseError(IGError):
    """IG returned a response that could not be interpreted."""
