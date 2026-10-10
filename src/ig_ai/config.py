from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .exceptions import ConfigurationError
from .health_contract import HealthContract
from .runtime_service import parse_custom_windows


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
    service_heartbeat_seconds: float = 30.0
    monitoring_mode: str = "24_7"
    runtime_timezone: str = "UTC"
    stale_data_seconds: float = 900.0
    market_status_refresh_seconds: float = 300.0
    custom_windows: tuple[str, ...] = ()

    @property
    def health_contract(self) -> HealthContract:
        return HealthContract(
            heartbeat_seconds=self.service_heartbeat_seconds,
            stale_data_seconds=self.stale_data_seconds,
        )

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
            heartbeat = float(env.get("IGAI_SERVICE_HEARTBEAT_SECONDS", "30"))
            stale_seconds = float(env.get("IGAI_STALE_DATA_SECONDS", "900"))
            runtime_timezone = env.get("IGAI_TIMEZONE", "UTC")
            ZoneInfo(runtime_timezone)
            market_status_refresh = float(env.get("IGAI_MARKET_STATUS_REFRESH_SECONDS", "300"))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("IG numeric settings or market timezone are invalid") from exc
        monitoring_mode = env.get("IGAI_MONITORING_MODE", "24_7").strip().upper()
        if monitoring_mode not in {"24_7", "MARKET_HOURS", "CUSTOM"}:
            raise ConfigurationError("IGAI_MONITORING_MODE must be 24_7, MARKET_HOURS, or CUSTOM")
        raw_custom_windows = env.get("IGAI_CUSTOM_WINDOWS", "").strip()
        try:
            custom_windows = parse_custom_windows(raw_custom_windows) if raw_custom_windows else ()
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        if monitoring_mode == "CUSTOM" and not custom_windows:
            raise ConfigurationError("IGAI_CUSTOM_WINDOWS is required when IGAI_MONITORING_MODE=CUSTOM")
        if timeout <= 0 or discovery_detail_budget <= 0 or reconnect < 0 or heartbeat <= 0 or stale_seconds <= 0 or market_status_refresh <= 0:
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
            service_heartbeat_seconds=heartbeat,
            monitoring_mode=monitoring_mode,
            runtime_timezone=runtime_timezone,
            stale_data_seconds=stale_seconds,
            market_status_refresh_seconds=market_status_refresh,
            custom_windows=custom_windows,
        )
