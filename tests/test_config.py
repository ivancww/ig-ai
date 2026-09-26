import pytest

from ig_ai.config import Settings
from ig_ai.exceptions import ConfigurationError


def test_missing_credentials_are_clear_and_secret_safe():
    with pytest.raises(ConfigurationError, match="IG_API_KEY") as error:
        Settings.from_env({"IG_USERNAME": "user", "IG_PASSWORD": "password"})
    assert "password" not in str(error.value).lower()


def test_demo_is_default():
    settings = Settings.from_env(
        {"IG_API_KEY": "key", "IG_USERNAME": "user", "IG_PASSWORD": "pass"}
    )
    assert settings.account_type == "DEMO"
