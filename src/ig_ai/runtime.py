from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .alerts import AlertEngine, monitor_state
from .candles import CandleAggregator
from .database import Database
from .forward import ForwardTestEngine
from .models import Instrument, as_utc
from .normalization import normalize_price_update
from .patterns import MultiTimeframeCoordinator, Phase2BEngine
from .streaming import IGStreamService
from .technical import TechnicalFeatureEngine

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
    session_started_at: datetime | None = None
    update_filter: Callable[[dict], bool] | None = None
    heartbeat_callback: Callable[[dict[str, datetime]], None] | None = None

    def __post_init__(self) -> None:
        if self.session_started_at is not None:
            self.session_started_at = as_utc(self.session_started_at)
        self.timeframes = ("15M", "1H", "4H", "1D")
        self.feature_engine = TechnicalFeatureEngine()
        self.phase2b_engine = Phase2BEngine(self.feature_engine)
        self.mtf_coordinator = MultiTimeframeCoordinator(self.phase2b_engine, max_history=600)
        self.phase2b_runs = 0
        self.phase2b_skipped_forming = 0
        self.alert_engine = AlertEngine()
        self.alert_engine.restore(self.database.list_alerts(limit=100000))
        self.alerts_emitted = 0
        self.forward_engine = ForwardTestEngine(self.database)
        self.forward_snapshots_written = 0
        self.forward_outcomes_completed = 0
        self.aggregators = {
            timeframe: CandleAggregator(timeframe, market_timezone=self.market_timezone)
            for timeframe in self.timeframes
        }
        self.observations_written = 0
        # Legacy 15M/1H write counters remain available; the complete metric
        # is candle_upserts, which covers all supported timeframes.
        self.candles_written = {"15M": 0, "1H": 0}
        self.candle_upserts = {timeframe: 0 for timeframe in self.timeframes}
        self.candle_counts = self.candle_upserts
        self.candle_upserts_by_instrument = {
            key: {timeframe: 0 for timeframe in self.timeframes} for key in self.instruments
        }
        # Compatibility alias for callers that used the old write-count name.
        self.candle_counts_by_instrument = self.candle_upserts_by_instrument
        self.finalized_candles_by_instrument = {
            key: {timeframe: 0 for timeframe in self.timeframes} for key in self.instruments
        }
        self.forming_candles_by_instrument = {
            key: {timeframe: 0 for timeframe in self.timeframes} for key in self.instruments
        }
        self._forming_keys: set[tuple[str, str, str]] = set()
        self.received_item_updates = {key: 0 for key in self.instruments}
        self.normalized_observations = {key: 0 for key in self.instruments}
        self.observations_persisted = {key: 0 for key in self.instruments}
        self.persistence_failures = {key: 0 for key in self.instruments}
        self.last_update: dict[str, datetime] = {}
        self._seen_observations: set[tuple] = set()
        self.safe_skip_diagnostics: list[dict[str, str]] = []
        self.runtime_failures: list[dict[str, str]] = []
        self.stale_forming_candles: list[dict[str, str]] = []
        self._restored_forming_keys: set[tuple[str, str, str]] = set()
        self._session_candle_starts: set[tuple[str, str, str]] = set()
        self.forward_snapshots_skipped_ineligible = 0
        # SQLite connections are deliberately owned by the thread that created
        # them.  SDK callbacks are delivered on an SDK-owned thread, so those
        # callbacks only enqueue immutable update dictionaries.
        self._database_owner_thread_id = threading.get_ident()
        for candle in self.database.load_forming_candles():
            aggregator = self.aggregators.get(candle.timeframe)
            if aggregator is not None and candle.instrument_id in self.instruments:
                self.database.mark_candle_audit_only(
                    candle, reason="restored_partial_without_session_coverage"
                )
                self.stale_forming_candles.append({
                    "instrument_id": candle.instrument_id,
                    "timeframe": candle.timeframe,
                    "start": candle.start.isoformat(),
                    "reason": "restored_partial_without_session_coverage",
                })

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
        self.normalized_observations[instrument_id] += 1
        signature = (observation.instrument_id, observation.timestamp, observation.bid, observation.offer)
        if signature in self._seen_observations:
            return {"observation_created": "false", "skip_reason": "duplicate_observation"}
        self._seen_observations.add(signature)
        if self.database.save_observation(observation) is False:
            return {"observation_created": "false", "skip_reason": "duplicate_observation"}
        self.observations_written += 1
        self.observations_persisted[instrument_id] += 1
        self.last_update[instrument_id] = observation.timestamp
        for timeframe, aggregator in self.aggregators.items():
            previous_forming = aggregator.forming(observation)
            for candle in aggregator.update(observation):
                self.database.save_candle(candle)
                if self.database.is_candle_eligible(candle):
                    self.database.save_features_for_candle(candle, self.feature_engine)
                    self._save_phase2b_for_candle(candle)
                self._record_candle_upsert(candle)
                self.finalized_candles_by_instrument[instrument_id][timeframe] += 1
            forming = aggregator.forming(observation)
            if forming:
                forming_key = (instrument_id, timeframe, forming.start.isoformat())
                if (
                    previous_forming is None
                    or previous_forming.start != forming.start
                ):
                    self._restored_forming_keys.discard(forming_key)
                    if self.session_started_at is None or forming.start >= self.session_started_at:
                        self._session_candle_starts.add(forming_key)
                    else:
                        self.database.mark_candle_audit_only(
                            forming, reason="partial_first_bucket_without_session_coverage"
                        )
                self._forming_keys.add((instrument_id, timeframe, forming.start.isoformat()))
                self.database.save_candle(forming)
                if self.database.is_candle_eligible(forming):
                    self.database.save_features_for_candle(forming, self.feature_engine)
                    self._save_phase2b_for_candle(forming)
                self._record_candle_upsert(forming)
                self._refresh_forming_count(instrument_id, timeframe)
        return {"observation_created": "true", "skip_reason": "none"}

    def _record_runtime_failure(self, stage: str, update: dict, error: Exception) -> None:
        instrument_id = str(update.get("instrument_id") or "unknown")
        instrument = self.instruments.get(instrument_id)
        affected = instrument.market_name if instrument is not None else instrument_id
        self.runtime_failures.append(
            {
                "failure_stage": stage,
                "exception_type": type(error).__name__,
                "affected_market": affected,
            }
        )

    def _process_update_safely(self, update: dict) -> None:
        if self.update_filter is not None and not self.update_filter(update):
            return
        instrument_id = str(update.get("instrument_id") or "")
        if instrument_id in self.received_item_updates and update.get("type") not in {"PROBE", "SUB", "UNSUB"}:
            self.received_item_updates[instrument_id] += 1
        try:
            self.on_update(update)
        except Exception as error:
            # Keep the stream worker alive for the other subscriptions and
            # retain only sanitized diagnostic fields, never exception text.
            self._record_runtime_failure("persistence/candle_pipeline", update, error)
            if instrument_id in self.persistence_failures:
                self.persistence_failures[instrument_id] += 1

    def _record_candle_upsert(self, candle) -> None:
        self.candle_upserts[candle.timeframe] += 1
        if candle.timeframe in self.candles_written:
            self.candles_written[candle.timeframe] += 1
        self.candle_upserts_by_instrument[candle.instrument_id][candle.timeframe] += 1
        if candle.is_closed:
            self._forming_keys.discard((candle.instrument_id, candle.timeframe, candle.start.isoformat()))
            self._refresh_forming_count(candle.instrument_id, candle.timeframe)

    def _refresh_forming_count(self, instrument_id: str, timeframe: str) -> None:
        self.forming_candles_by_instrument[instrument_id][timeframe] = sum(
            1 for key in self._forming_keys if key[:2] == (instrument_id, timeframe)
        )

    def _save_phase2b_for_candle(self, candle) -> None:
        if not self.database.is_candle_eligible(candle):
            return
        if not candle.is_closed and candle.observation_count > 1 and candle.observation_count % 10 != 0:
            self.phase2b_skipped_forming += 1
            return
        histories = {
            timeframe: self.database.list_candles(
                candle.instrument_id,
                timeframe,
                through=candle.start.isoformat(),
                limit=self.mtf_coordinator.max_history,
            )
            for timeframe in self.timeframes
        }
        coordinated = self.mtf_coordinator.analyze(histories, target_time=candle.start, target_timeframe=candle.timeframe)
        self.phase2b_runs += 1
        analysis = coordinated["timeframes"].get(candle.timeframe)
        if analysis is not None:
            analysis["direction_reversal"] = coordinated["direction_reversal"]
            self.database.save_phase2b_analysis(analysis)
            direction = dict(coordinated["direction_score"])
            # A direction snapshot is a primary 1H model record.  Early
            # runtime ticks may not yet have a usable 1H analysis; those
            # Phase 2B records remain valid without inventing a snapshot.
            if direction.get("instrument") and direction.get("candle_timestamp"):
                direction["trigger"] = {"timeframe": candle.timeframe, "candle_timestamp": candle.start.isoformat(), "candle_state": "CLOSED" if candle.is_closed else "FORMING"}
                self.database.save_direction_snapshot(direction)
                state = monitor_state(direction, coordinated)
                previous = self.database.get_monitor_state(state["instrument"])
                alerts = self.alert_engine.evaluate(previous, state)
                self.alerts_emitted += self.database.save_monitor_evaluation(state, alerts)
            if candle.timeframe == "1H" and candle.is_closed:
                candle_key = (candle.instrument_id, candle.timeframe, candle.start.isoformat())
                if candle_key in self._session_candle_starts and candle_key not in self._restored_forming_keys:
                    self.forward_engine.record_snapshot(
                        reference_candle=candle, coordinated=coordinated
                    )
                    self.forward_snapshots_written += int(self.forward_engine.last_inserted)
                else:
                    self.forward_snapshots_skipped_ineligible += 1
                    self.safe_skip_diagnostics.append({
                        "instrument_id": candle.instrument_id,
                        "timeframe": candle.timeframe,
                        "reason": "ineligible_forward_reference",
                    })
            # Outcomes are calculated only from complete 15M candles.  The
            # exact-end requirement in OutcomeCalculator keeps all other
            # horizons PENDING until their future data actually exists.
            if candle.is_closed and candle.timeframe == "15M":
                self.forward_outcomes_completed += self.forward_engine.update_outcomes(
                    instrument_id=candle.instrument_id,
                    candles=self.database.list_candles(candle.instrument_id, "15M"),
                )

    @staticmethod
    def _exit_reason(stream: IGStreamService) -> str | None:
        stats = getattr(stream, "stats", None)
        diagnostics = getattr(stats, "diagnostics", None)
        return getattr(diagnostics, "exit_reason", None)

    def run_for(self, stream: IGStreamService, duration: float) -> None:
        if threading.get_ident() != self._database_owner_thread_id:
            raise RuntimeError("PersistedStream.run_for must run on the database owner thread")
        updates: queue.Queue[dict | None] = queue.Queue()

        def enqueue_update(update: dict) -> None:
            # This function runs on the stream/SDK service worker.  It must not
            # touch SQLite or candle state.
            updates.put(update)

        worker = threading.Thread(target=stream.run, args=(enqueue_update,), daemon=True)
        started = time.monotonic()
        deadline = started + duration
        worker.start()
        while time.monotonic() < deadline or not updates.empty():
            if self.heartbeat_callback is not None:
                self.heartbeat_callback(self.last_update)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostics = getattr(getattr(stream, "stats", None), "diagnostics", None)
                if not getattr(diagnostics, "duration_expired", False):
                    stream.mark_duration_expired()
                    stream.stop(reason="duration")
                remaining = 0.5
            if not worker.is_alive() and not updates.empty():
                remaining = min(0.25, max(0.01, remaining))
            try:
                update = updates.get(timeout=min(0.25, max(0.01, remaining)))
            except queue.Empty:
                continue
            self._process_update_safely(update)
            if self._exit_reason(stream) in {"USER_INTERRUPT", "CONNECTION_TERMINATED", "SDK_FATAL_ERROR", "RUNTIME_FAILURE"}:
                break
        if self._exit_reason(stream) is None:
            diagnostics = getattr(getattr(stream, "stats", None), "diagnostics", None)
            if diagnostics is None:
                stream.stop(reason="runtime")
            elif worker.is_alive() or getattr(diagnostics, "duration_expired", False) or time.monotonic() >= deadline:
                if time.monotonic() >= deadline:
                    stream.mark_duration_expired()
                stream.stop(reason="duration" if time.monotonic() >= deadline else "runtime")
        worker.join(timeout=5)
        while True:
            try:
                self._process_update_safely(updates.get_nowait())
            except queue.Empty:
                break
        # Flush stores a forming candle as open. It deliberately does not
        # overwrite a completed candle as closed.
        for aggregator in self.aggregators.values():
            for candle in aggregator.flush():
                self.database.save_candle(candle)
                if self.database.is_candle_eligible(candle):
                    self.database.save_features_for_candle(candle, self.feature_engine)
                    self._save_phase2b_for_candle(candle)
                self._forming_keys.add((candle.instrument_id, candle.timeframe, candle.start.isoformat()))
                self._record_candle_upsert(candle)
                self._refresh_forming_count(candle.instrument_id, candle.timeframe)
        elapsed = time.monotonic() - started
        if self._exit_reason(stream) is None:
            diagnostics = getattr(getattr(stream, "stats", None), "diagnostics", None)
            if diagnostics is not None:
                diagnostics.exit_reason = "DURATION_COMPLETE" if elapsed >= duration else "CONNECTION_TERMINATED"
        self.runtime_audit = {
            "requested_duration": duration,
            "actual_elapsed": elapsed,
            "exit_reason": self._exit_reason(stream) or ("DURATION_COMPLETE" if elapsed >= duration else "CONNECTION_TERMINATED"),
            "last_successful_item_update": max(
                getattr(getattr(stream, "stats", None), "last_update", {}).values(), default=None
            ),
            "last_persistence_success": max(self.last_update.values(), default=None),
        }
