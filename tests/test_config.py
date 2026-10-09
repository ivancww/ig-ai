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


def test_discovery_detail_budget_is_configurable_and_positive():
    settings = Settings.from_env(
        {
            "IG_API_KEY": "key",
            "IG_USERNAME": "user",
            "IG_PASSWORD": "pass",
            "IG_DISCOVERY_DETAIL_BUDGET": "5",
        }
    )
    assert settings.discovery_detail_budget == 5

    with pytest.raises(ConfigurationError, match="discovery detail budget"):
        Settings.from_env(
            {
                "IG_API_KEY": "key",
                "IG_USERNAME": "user",
                "IG_PASSWORD": "pass",
                "IG_DISCOVERY_DETAIL_BUDGET": "0",
            }
        )


def test_market_status_refresh_interval_is_configurable_and_positive():
    settings = Settings.from_env(
        {
            "IG_API_KEY": "key",
            "IG_USERNAME": "user",
            "IG_PASSWORD": "pass",
            "IGAI_MARKET_STATUS_REFRESH_SECONDS": "120",
        }
    )
    assert settings.market_status_refresh_seconds == 120
    with pytest.raises(ConfigurationError):
        Settings.from_env(
            {
                "IG_API_KEY": "key",
                "IG_USERNAME": "user",
                "IG_PASSWORD": "pass",
                "IGAI_MARKET_STATUS_REFRESH_SECONDS": "0",
            }
        )
