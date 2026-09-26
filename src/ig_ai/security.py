import logging

SENSITIVE_NAMES = (
    "api_key",
    "password",
    "cst",
    "x-security-token",
    "security_token",
    "session_token",
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(name in message.lower() for name in SENSITIVE_NAMES):
            record.msg = "redacted sensitive log message"
            record.args = ()
        return True
