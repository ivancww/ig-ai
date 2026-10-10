"""One shared contract for runtime heartbeat and market freshness evidence."""

from __future__ import annotations

from dataclasses import dataclass

HEALTH_CONTRACT_VERSION = "ig-ai-health-v1"
HEARTBEAT_MAX_AGE_MULTIPLIER = 3.0


@dataclass(frozen=True)
class HealthContract:
    """Thresholds shared by runtime, Web, and deployment verification.

    The heartbeat window intentionally preserves the existing runtime rule:
    three configured heartbeat intervals.  It is defined once here so an
    operator cannot accidentally deploy a different readiness interpretation.
    """

    heartbeat_seconds: float = 30.0
    stale_data_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.heartbeat_seconds <= 0 or self.stale_data_seconds <= 0:
            raise ValueError("heartbeat and stale-data intervals must be positive")

    @property
    def heartbeat_max_age_seconds(self) -> float:
        return self.heartbeat_seconds * HEARTBEAT_MAX_AGE_MULTIPLIER

    def as_dict(self) -> dict[str, float | str]:
        return {
            "version": HEALTH_CONTRACT_VERSION,
            "heartbeat_seconds": self.heartbeat_seconds,
            "heartbeat_max_age_seconds": self.heartbeat_max_age_seconds,
            "stale_data_seconds": self.stale_data_seconds,
        }
