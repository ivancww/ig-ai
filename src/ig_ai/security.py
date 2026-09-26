import logging

SENSITIVE_NAMES = (
    "api_key",
    "password",
    "cst",
    "x-security-token",
    "security_token",
    "session_token",
)


def redact_text(message: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove known secret values and credential-shaped log fields."""
    result = message
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = redact_text(record.getMessage(), self.secrets)
        if message != record.getMessage() or any(name in message.lower() for name in SENSITIVE_NAMES):
            record.msg = "redacted sensitive log message"
            record.args = ()
        return True
