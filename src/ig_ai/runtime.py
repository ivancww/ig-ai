from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .candles import CandleAggregator
from .database import Database
from .models import Instrument
from .normalization import normalize_price_update
from .streaming import IGStreamService

log = logging.getLogger(__name__)


class Runtime:
    """Small headless runner boundary; orchestration remains injectable/testable."""

    def __init__(self, stream: IGStreamService, on_update: Callable[[dict], None]):
        self.stream = stream
        self.on_update = on_update

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stream.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stream.stop())

    def run(self) -> None:
        self.install_signal_handlers()
        log.info("market-data runtime started")
        self.stream.run(self.on_update)
        log.info("market-data runtime stopped")


@dataclass
class PersistedStream:
    """Read-only stream sink: normalize, persist, and aggregate without trading."""

    database: Database
    instruments: dict[str, Instrument]
    market_timezone: str = "UTC"

    def __post_init__(self) -> None:
        self.aggregators = {timeframe: CandleAggregator(timeframe, market_timezone=self.market_timezone) for timeframe in ("15M", "1H")}
        self.observations_written = 0
        self.candles_written = {timeframe: 0 for timeframe in self.aggregators}
        self.last_update: dict[str, datetime] = {}
        self._seen_observations: set[tuple] = set()
        self.safe_skip_diagnostics: list[dict[str, str]] = []

    def on_update(self, update: dict) -> dict[str, str]:
        if update.get("type") in {"PROBE", "SUB", "UNSUB"}:
            return {"observation_created": "false", "skip_reason": "lifecycle_message"}
        instrument_id = str(update.get("instrument_id") or "")
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            self.safe_skip_diagnostics.append({"reason": "unknown_instrument"})
            return {"observation_created": "false", "skip_reason": "unknown_instrument"}
        timestamp_value = (
            update.get("UPDATE_TIME")
            or update.get("UPDATE_TIMESTAMP")
            or update.get("PROVIDER_TIMESTAMP")
            or update.get("TIMESTAMP")
            or update.get("timestamp")
        )
        if timestamp_value in (None, ""):
            self.safe_skip_diagnostics.append({"instrument_id": instrument_id, "reason": "missing_timestamp"})
            return {"observation_created": "false", "skip_reason": "missing_timestamp"}
        try:
            observation = normalize_price_update(
                update,
                instrument_id=instrument.instrument_id,
                epic=instrument.epic,
                market_name=instrument.market_name,
            )
        except (TypeError, ValueError):
            self.safe_skip_diagnostics.append({"instrument_id": instrument_id, "reason": "invalid_timestamp_or_price"})
            return {"observation_created": "false", "skip_reason": "invalid_timestamp_or_price"}
        if observation.mid is None:
            self.safe_skip_diagnostics.append({"instrument_id": instrument_id, "reason": "missing_bid_or_ask"})
            return {"observation_created": "false", "skip_reason": "missing_bid_or_ask"}
        signature = (observation.instrument_id, observation.timestamp, observation.bid, observation.offer)
        if signature in self._seen_observations:
            return {"observation_created": "false", "skip_reason": "duplicate_observation"}
        self._seen_observations.add(signature)
        self.database.save_observation(observation)
        self.observations_written += 1
        self.last_update[instrument_id] = observation.timestamp
        for timeframe, aggregator in self.aggregators.items():
            for candle in aggregator.update(observation):
                self.database.save_candle(candle)
                self.candles_written[timeframe] += 1
            forming = aggregator.forming(observation)
            if forming:
                self.database.save_candle(forming)
                self.candles_written[timeframe] += 1
        return {"observation_created": "true", "skip_reason": "none"}

    def run_for(self, stream: IGStreamService, duration: float) -> None:
        worker = threading.Thread(target=stream.run, args=(self.on_update,), daemon=True)
        worker.start()
        deadline = time.monotonic() + duration
        while worker.is_alive() and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        if worker.is_alive():
            stream.mark_duration_expired()
            stream.stop(reason="duration")
        else:
            stream.stop(reason="runtime")
        worker.join(timeout=5)
        # Flush stores a forming candle as open. It deliberately does not
        # overwrite a completed candle as closed.
        for aggregator in self.aggregators.values():
            for candle in aggregator.flush():
                self.database.save_candle(candle)
                self.candles_written[candle.timeframe] += 1
