import logging

from ig_ai.security import SecretRedactionFilter


def test_sensitive_log_message_is_redacted(caplog):
    logger = logging.getLogger("security-test")
    logger.addFilter(SecretRedactionFilter())
    with caplog.at_level(logging.INFO):
        logger.info("request included X-SECURITY-TOKEN")
    assert "X-SECURITY-TOKEN" not in caplog.text
    assert "redacted" in caplog.text
