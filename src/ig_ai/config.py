from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .exceptions import ConfigurationError


@dataclass(frozen=True)
class Settings:
    api_key: str
    username: str
    password: str
    account_type: str = "DEMO"
    api_base_url: str = "https://demo-api.ig.com/gateway/deal"
    database_path: str = "data/ig_ai.sqlite3"
    request_timeout_seconds: float = 15.0
    stream_reconnect_seconds: float = 5.0
    market_timezone: str = "Asia/Hong_Kong"
    discovery_detail_budget: int = 3

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None, *, require_credentials: bool = True
    ) -> Settings:
        env = os.environ if environ is None else environ
        values = {
            key: env.get(key, "").strip() for key in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD")
        }
        if require_credentials and any(not value for value in values.values()):
            missing = ", ".join(key for key, value in values.items() if not value)
            raise ConfigurationError(
                f"Missing required IG configuration: {missing}. Set them in the environment."
            )
        account_type = env.get("IG_ACCOUNT_TYPE", "DEMO").strip().upper() or "DEMO"
        if account_type not in {"DEMO", "LIVE"}:
            raise ConfigurationError("IG_ACCOUNT_TYPE must be DEMO or LIVE")
        default_url = (
            "https://demo-api.ig.com/gateway/deal"
            if account_type == "DEMO"
            else "https://api.ig.com/gateway/deal"
        )
        try:
            timeout = float(env.get("IG_REQUEST_TIMEOUT_SECONDS", "15"))
            discovery_detail_budget = int(env.get("IG_DISCOVERY_DETAIL_BUDGET", "3"))
            reconnect = float(env.get("IG_STREAM_RECONNECT_SECONDS", "5"))
            timezone = env.get("IG_MARKET_TIMEZONE", "Asia/Hong_Kong")
            ZoneInfo(timezone)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("IG numeric settings or market timezone are invalid") from exc
        if timeout <= 0 or discovery_detail_budget <= 0 or reconnect < 0:
            raise ConfigurationError(
                "IG timeout must be positive, discovery detail budget must be positive, "
                "and reconnect delay non-negative"
            )
        database_path = env.get("IG_DATABASE_PATH", "data/ig_ai.sqlite3").strip()
        if not database_path:
            raise ConfigurationError("IG_DATABASE_PATH must not be empty")
        return cls(
            api_key=values["IG_API_KEY"],
            username=values["IG_USERNAME"],
            password=values["IG_PASSWORD"],
            account_type=account_type,
            api_base_url=env.get("IG_API_BASE_URL", default_url).rstrip("/"),
            database_path=Path(database_path),
            request_timeout_seconds=timeout,
            discovery_detail_budget=discovery_detail_budget,
            stream_reconnect_seconds=reconnect,
            market_timezone=timezone,
        )
