"""Deterministic historical outcome and research foundation.

Research snapshots are point-in-time records.  Future candles are accepted
only by :class:`OutcomeCalculator` when completing outcomes, never while
constructing technical state.  All percentages are descriptive sample
statistics with an explicit sample count; they are not probabilities.
"""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .direction import DirectionScoreEngine
from .models import Candle
from .patterns import DirectionReversalEngine, Phase2BEngine

RESEARCH_ENGINE_VERSION = "research_engine_v1"
RESEARCH_SOURCE = "ig_cfd"
HORIZON_MINUTES = {"15M": 15, "1H": 60, "2H": 120, "4H": 240, "8H": 480, "1D": 1440}
RESEARCH_WINDOWS_YEARS = (1, 3, 5)
# The score is technical evidence in the closed interval [0, 100], not a
# probability.  Keep the historical 5-point upper buckets and add explicit
# lower-domain buckets so a valid low score is never represented as missing.
SCORE_BUCKETS = (
    (0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
    (50, 55), (55, 60), (60, 65), (65, 70), (70, 75),
    (75, 80), (80, 85), (85, 90), (90, 100),
)
DESCRIPTIVE_RESEARCH_LABEL = "DESCRIPTIVE / IN-SAMPLE RESEARCH"
NOT_CALIBRATED_PROBABILITY_LABEL = "NOT CALIBRATED PROBABILITY"
NOT_OUT_OF_SAMPLE_LABEL = "NOT OUT-OF-SAMPLE PERFORMANCE"


@dataclass(frozen=True)
class ResearchConfig:
    default_window: str = "3Y"
    flat_threshold: float = 0.001
    meaningful_reversal_threshold: float = 0.002
    early_sample_count: int = 30
    usable_sample_count: int = 100
    version: str = RESEARCH_ENGINE_VERSION


def score_bucket(score: float | int | None) -> str | None:
    if score is None:
        return None
    value = float(score)
    if value < 0 or value > 100:
        return None
    for lower, upper in SCORE_BUCKETS:
        if lower <= value < upper or (upper == 100 and lower <= value <= upper):
            return f"{lower}–{upper}"
    return None


def sample_quality(sample_count: int, config: ResearchConfig | None = None) -> str:
    config = config or ResearchConfig()
    if sample_count < config.early_sample_count:
        return "INSUFFICIENT_SAMPLE"
    if sample_count < config.usable_sample_count:
        return "EARLY_SAMPLE"
    return "USABLE_SAMPLE"


def window_start(now: datetime, window: str) -> datetime:
    if window not in {f"{years}Y" for years in RESEARCH_WINDOWS_YEARS}:
        raise ValueError("window must be 1Y, 3Y, or 5Y")
    years = int(window[:-1])
    return now.astimezone(UTC) - timedelta(days=365 * years)


REGIME_METHODOLOGY_VERSION = "technical_regime_v1"


def regime_tags(snapshot: dict[str, Any], *, volatility_comparison: str | None = None) -> dict[str, str]:
    """Return independent technical tags when context exists.

    Without technical context this explicitly falls back to MODEL_STATE tags;
    those are not independent validation strata.
    """
    technical = snapshot.get("technical_context") or snapshot.get("context") or {}
    moving = technical.get("moving_averages", {})
    ema20 = moving.get("ema20", {}).get("price_relation")
    ema50 = moving.get("ema50", {}).get("price_relation")
    if ema20 == "above" and ema50 == "above":
        trend = "TRENDING_UP"
    elif ema20 == "below" and ema50 == "below":
        trend = "TRENDING_DOWN"
    elif not technical:
        direction = snapshot.get("direction")
        trend = f"MODEL_STATE_{direction}" if direction in {"UP", "DOWN"} else "MODEL_STATE_NEUTRAL"
    else:
        trend = "RANGE_NEUTRAL"
    volatility = {"higher_than_recent": "HIGH", "lower_than_recent": "LOW"}.get(volatility_comparison, "NORMAL")
    return {"trend_regime": trend, "volatility_regime": volatility}


def _direction_outcome(reference: float, future: float, threshold: float) -> str:
    change = (future - reference) / reference if reference else 0.0
    if change > threshold:
        return "UP"
    if change < -threshold:
        return "DOWN"
    return "FLAT"


def _candle_at_exact_end(candles: list[Candle], target: datetime) -> Candle | None:
    return next((candle for candle in candles if candle.is_closed and candle.end == target), None)


class OutcomeCalculator:
    """Calculate outcomes only from candles after an already recorded snapshot."""

    def __init__(self, config: ResearchConfig | None = None):
        self.config = config or ResearchConfig()

    def calculate(self, *, reference_price: float, reference_time: datetime, direction: str | None, candles: list[Candle], horizon: str) -> dict[str, Any]:
        if horizon not in HORIZON_MINUTES:
            raise ValueError(f"unsupported horizon: {horizon}")
        reference_time = reference_time.astimezone(UTC)
        target = reference_time + timedelta(minutes=HORIZON_MINUTES[horizon])
        # Reference time is the END of the closed snapshot candle.  The
        # interval is exactly (T, T+horizon]; a candle is usable only after
        # its complete interval has closed and only when its end is within it.
        eligible = [candle for candle in candles if candle.is_closed and candle.end > reference_time and candle.end <= target]
        future = _candle_at_exact_end(candles, target)
        if future is None:
            return {"status": "PENDING", "future_timestamp": None, "future_price": None, "absolute_move": None, "percentage_move": None, "direction_outcome": None, "mfe": None, "mae": None, "time_to_mfe": None, "time_to_mae": None, "time_to_reversal": None, "continuation_duration": None, "direction_side_held": None}
        future_price = float(future.close)
        signed_moves = []
        for candle in eligible:
            high_move = float(candle.high) - reference_price
            low_move = float(candle.low) - reference_price
            signed_moves.append((high_move, low_move, candle))
        if direction == "DOWN":
            favourable = [-low for _high, low, _candle in signed_moves]
            adverse = [-high for high, _low, _candle in signed_moves]
        else:
            favourable = [high for high, _low, _candle in signed_moves]
            adverse = [low for _high, low, _candle in signed_moves]
        mfe = max(favourable, default=0.0)
        mae = min(adverse, default=0.0)
        mfe_candle = signed_moves[favourable.index(mfe)][2] if signed_moves else None
        mae_candle = signed_moves[adverse.index(mae)][2] if signed_moves else None
        side_held = None if direction not in {"UP", "DOWN"} else all(
            (float(candle.close) - reference_price >= 0 if direction == "UP" else reference_price - float(candle.close) >= 0)
            for candle in eligible
        )
        reversal = None
        for candle in eligible:
            change = (float(candle.close) - reference_price) / reference_price if reference_price else 0.0
            if direction == "UP" and change <= -self.config.meaningful_reversal_threshold:
                reversal = candle
                break
            if direction == "DOWN" and change >= self.config.meaningful_reversal_threshold:
                reversal = candle
                break
        return {
            "status": "COMPLETE", "future_timestamp": future.end.isoformat(), "future_price": future_price,
            "absolute_move": future_price - reference_price, "percentage_move": (future_price - reference_price) / reference_price * 100 if reference_price else None,
            "direction_outcome": _direction_outcome(reference_price, future_price, self.config.flat_threshold),
            "mfe": mfe, "mae": mae, "time_to_mfe": (mfe_candle.end - reference_time).total_seconds() if mfe_candle else None,
            "time_to_mae": (mae_candle.end - reference_time).total_seconds() if mae_candle else None,
            "time_to_reversal": (reversal.end - reference_time).total_seconds() if reversal else None,
            "continuation_duration": (reversal.end - reference_time).total_seconds() if reversal else (future.end - reference_time).total_seconds(),
            "direction_side_held": side_held,
        }


class HistoricalReplay:
    """Replay closed historical candles with point-in-time bounded inputs.

    This is an ingestion boundary for a future backfill job.  It does not
    fetch data or persist results by itself.  Each 1H state uses candles whose
    complete interval ended no later than that 1H candle's end.
    """

    def __init__(self, *, max_history: int = 600):
        self.max_history = max_history
        self.phase2b = Phase2BEngine()

    def state_for_aligned(self, target: Candle, aligned: dict[str, list[Candle]]) -> dict[str, Any]:
        if any(candle.end > target.end for candles in aligned.values() for candle in candles):
            raise ValueError("replay model input contains a candle after information_time")
        bounded = {
            timeframe: [candle for candle in candles if candle.is_closed and candle.end <= target.end][-self.max_history:]
            for timeframe, candles in aligned.items()
        }
        analyses = {timeframe: self.phase2b.analyze(items) for timeframe, items in bounded.items() if items}
        features = {timeframe: {"structure": analysis["structure"], "macd": analysis["context"]["macd"], "rsi14": analysis["context"]["rsi"]} for timeframe, analysis in analyses.items()}
        divergences = {timeframe: analysis["divergences"] for timeframe, analysis in analyses.items()}
        direction = DirectionScoreEngine().analyze(analyses)
        direction["model_reference"] = {"timeframe": "1H", "candle_timestamp": target.start.isoformat(), "candle_state": "CLOSED", "information_time": target.end.isoformat()}
        direction["replay_reference_time"] = target.end.isoformat()
        direction["direction_reversal"] = DirectionReversalEngine().classify(features, divergences)
        return {"state": direction, "reference_candle": target, "histories": bounded}

    def direction_states(self, candles_by_timeframe: dict[str, list[Candle]]) -> list[dict[str, Any]]:
        histories = {timeframe: sorted((candle for candle in candles if candle.is_closed), key=lambda candle: candle.end) for timeframe, candles in candles_by_timeframe.items()}
        primary = histories.get("1H", [])
        end_indexes = {timeframe: [candle.end for candle in candles] for timeframe, candles in histories.items()}
        output = []
        for target in primary:
            aligned = {}
            for timeframe, candles in histories.items():
                end_index = bisect_right(end_indexes[timeframe], target.end)
                aligned[timeframe] = candles[max(0, end_index - self.max_history):end_index]
            output.append(self.state_for_aligned(target, aligned))
        return output


class ResearchEngine:
    def __init__(self, database, config: ResearchConfig | None = None):
        self.database = database
        self.config = config or ResearchConfig()
        self.calculator = OutcomeCalculator(self.config)

    def record_snapshot(self, *, instrument_id: str, timeframe: str, snapshot_timestamp: str, reference_price: float, snapshot_type: str, state: dict[str, Any], reference_time: str | None = None, source_identity: str | None = None, pattern_name: str | None = None, pattern_lifecycle: str | None = None, pattern_instance_id: str | None = None, divergence_indicator: str | None = None, divergence_direction: str | None = None, alert_type: str | None = None, alert_id: str | None = None, alert_event_identity: str | None = None, volatility_comparison: str | None = None) -> str:
        direction = state.get("direction")
        identity = pattern_instance_id or alert_id or alert_event_identity or divergence_indicator or ""
        snapshot_id = hashlib.sha256(f"{self.config.version}:{instrument_id}:{timeframe}:{snapshot_type}:{snapshot_timestamp}:{identity}:{pattern_name or ''}:{pattern_lifecycle or ''}:{alert_type or ''}".encode()).hexdigest()
        regimes = regime_tags(state, volatility_comparison=volatility_comparison)
        agreement = state.get("timeframe_agreement") or {}
        source_identity = source_identity or self.database.research_source_identity(instrument_id)
        payload = dict(state)
        payload["research_engine_version"] = self.config.version
        self.database.save_research_snapshot({"snapshot_id": snapshot_id, "instrument_id": instrument_id, "timeframe": timeframe, "snapshot_timestamp": snapshot_timestamp, "reference_time": reference_time or snapshot_timestamp, "snapshot_type": snapshot_type, "source_identity": source_identity, "reference_price": reference_price, "direction": direction, "up_score": state.get("up_score"), "down_score": state.get("down_score"), "coverage": state.get("coverage"), "trend_stage": state.get("trend_stage"), "reversal_risk": (state.get("reversal_risk") or {}).get("category"), "holding_window": state.get("holding_window"), "agreement": agreement.get("status"), "four_hour_alignment": (agreement.get("states") or {}).get("4H"), "pattern_name": pattern_name, "pattern_lifecycle": pattern_lifecycle, "instance_identity": pattern_instance_id, "divergence_indicator": divergence_indicator, "divergence_direction": divergence_direction, "alert_type": alert_type, "alert_id": alert_id, "alert_event_identity": alert_event_identity, "score_bucket": score_bucket(state.get("up_score") if direction == "UP" else state.get("down_score")), "trend_regime": regimes["trend_regime"], "volatility_regime": regimes["volatility_regime"], "context_json": json.dumps(payload, sort_keys=True, separators=(",", ":")), "feature_version": state.get("feature_schema_version"), "pattern_version": state.get("pattern_schema_version"), "direction_version": state.get("score_version"), "alert_version": state.get("alert_engine_version"), "research_version": self.config.version})
        return snapshot_id

    def complete_snapshot(self, snapshot_id: str, *, reference_time: datetime, reference_price: float, direction: str | None, candles: list[Candle], horizons: tuple[str, ...] = tuple(HORIZON_MINUTES)) -> int:
        outcomes = {horizon: self.calculator.calculate(reference_price=reference_price, reference_time=reference_time, direction=direction, candles=candles, horizon=horizon) for horizon in horizons}
        self.database.save_research_outcomes(snapshot_id, outcomes)
        return len(outcomes)

    def record_pattern(self, *, pattern: dict[str, Any], reference_price: float, state: dict[str, Any]) -> str:
        name = pattern.get("pattern") or ""
        divergence = "RSI" if "RSI" in name else "MACD" if "MACD" in name else None
        divergence_direction = "BULLISH" if "Bullish" in name else "BEARISH" if "Bearish" in name else None
        return self.record_snapshot(instrument_id=pattern["instrument"], timeframe=pattern["timeframe"], snapshot_timestamp=pattern.get("start") or pattern["candle_timestamp"], reference_time=pattern.get("end") or pattern.get("start") or pattern["candle_timestamp"], reference_price=reference_price, snapshot_type="DIVERGENCE" if divergence else "PATTERN", state=state, pattern_name=pattern.get("pattern"), pattern_lifecycle=pattern.get("lifecycle"), pattern_instance_id=pattern.get("instance_id"), divergence_indicator=divergence, divergence_direction=divergence_direction, volatility_comparison=(state.get("context") or {}).get("atr", {}).get("recent_comparison"))

    def record_alert(self, *, alert: dict[str, Any], reference_price: float, state: dict[str, Any]) -> str:
        model = state.get("model_reference") or {}
        return self.record_snapshot(instrument_id=alert["instrument"], timeframe=model.get("timeframe", "1H"), snapshot_timestamp=alert.get("model_reference_time") or alert["created_at"], reference_time=model.get("information_time") or alert.get("model_reference_time") or alert["created_at"], reference_price=reference_price, snapshot_type="ALERT", state=state, alert_type=alert.get("alert_type"), alert_id=alert.get("alert_id"), alert_event_identity=alert.get("event_identity"))

    def run_direction_research(self, *, instrument_id: str, window: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        window = window or self.config.default_window
        now = now or datetime.now(UTC)
        start = window_start(now, window)
        rows = self.database.get_direction_snapshots(instrument_id, start.isoformat())
        recorded = 0
        completed = 0
        primary_candles = self.database.list_candles(instrument_id, "1H")
        outcome_candles = self.database.list_candles(instrument_id, "15M")
        primary_by_start = {candle.start.isoformat(): candle for candle in primary_candles}
        for state in rows:
            reference_start = datetime.fromisoformat(state["candle_timestamp"])
            reference_candle = primary_by_start.get(state["candle_timestamp"])
            if reference_start < start or state.get("candle_state") != "CLOSED" or reference_candle is None or not reference_candle.is_closed:
                continue
            reference_time = reference_candle.end
            snapshot_id = self.record_snapshot(instrument_id=instrument_id, timeframe="1H", snapshot_timestamp=state["candle_timestamp"], reference_time=reference_time.isoformat(), reference_price=float(reference_candle.close), snapshot_type="DIRECTION", state=state)
            completed += self.complete_snapshot(snapshot_id, reference_time=reference_time, reference_price=float(reference_candle.close), direction=state.get("direction"), candles=outcome_candles)
            recorded += 1
        available = self.database.available_history(instrument_id, "1H", requested_window=window)
        return {"window": window, "requested_start": start.isoformat(), "recorded_snapshots": recorded, "outcomes_written": completed, "available_history": available}

    def run_replay(self, *, instrument_id: str, candles_by_timeframe: dict[str, list[Candle]], window: str | None = None, now: datetime | None = None) -> dict[str, int]:
        """Replay supplied historical candles and persist reproducible states."""
        window = window or self.config.default_window
        start = window_start(now or datetime.now(UTC), window)
        outcome_candles = sorted(candles_by_timeframe.get("15M", []), key=lambda candle: candle.end)
        recorded = completed = 0
        for item in HistoricalReplay().direction_states(candles_by_timeframe):
            candle = item["reference_candle"]
            if candle.start < start:
                continue
            state = item["state"]
            snapshot_id = self.record_snapshot(instrument_id=instrument_id, timeframe="1H", snapshot_timestamp=candle.start.isoformat(), reference_time=candle.end.isoformat(), reference_price=float(candle.close), snapshot_type="DIRECTION_REPLAY", state=state)
            completed += self.complete_snapshot(snapshot_id, reference_time=candle.end, reference_price=float(candle.close), direction=state.get("direction"), candles=outcome_candles)
            recorded += 1
        return {"recorded_snapshots": recorded, "outcomes_written": completed}

    def run_pattern_research(self, *, instrument_id: str, timeframe: str, window: str | None = None, now: datetime | None = None) -> dict[str, int]:
        window = window or self.config.default_window
        start = window_start(now or datetime.now(UTC), window)
        observations = self.database.get_pattern_observations(instrument_id, timeframe, start.isoformat())
        pattern_candles = {candle.start.isoformat(): candle for candle in self.database.list_candles(instrument_id, timeframe)}
        outcome_candles = self.database.list_candles(instrument_id, "15M")
        recorded = completed = 0
        for item in observations:
            candle = pattern_candles.get(item["candle_start"])
            if candle is None or not item["is_closed"]:
                continue
            primary = self.database.get_direction_snapshot_at(instrument_id, item["candle_start"]) or {}
            state = {**primary, "direction": primary.get("direction"), "context": item["observation"].get("context", {})}
            pattern = {**item["observation"], "instrument": instrument_id, "timeframe": timeframe, "instance_id": item["instance_id"], "lifecycle": item["lifecycle"], "start": item["candle_start"], "end": candle.end.isoformat()}
            snapshot_id = self.record_pattern(pattern=pattern, reference_price=float(candle.close), state=state)
            completed += self.complete_snapshot(snapshot_id, reference_time=candle.end, reference_price=float(candle.close), direction=state.get("direction"), candles=outcome_candles)
            recorded += 1
        return {"recorded_snapshots": recorded, "outcomes_written": completed}

    def run_alert_research(self, *, instrument_id: str, window: str | None = None, now: datetime | None = None) -> dict[str, int]:
        window = window or self.config.default_window
        start = window_start(now or datetime.now(UTC), window)
        alerts = [alert for alert in self.database.list_alerts(instrument_id=instrument_id, limit=100000) if (alert.get("model_reference_time") or alert.get("created_at", "")) >= start.isoformat()]
        primary_candles = {candle.start.isoformat(): candle for candle in self.database.list_candles(instrument_id, "1H")}
        outcome_candles = self.database.list_candles(instrument_id, "15M")
        recorded = completed = 0
        for alert in alerts:
            reference_start = alert.get("model_reference_time")
            candle = primary_candles.get(reference_start)
            if candle is None or not candle.is_closed:
                continue
            state = {"instrument": instrument_id, "direction": alert.get("current_direction"), "up_score": alert.get("current_score") if alert.get("current_direction") == "UP" else None, "down_score": alert.get("current_score") if alert.get("current_direction") == "DOWN" else None, "trend_stage": alert.get("current_trend_stage"), "reversal_risk": {"category": alert.get("current_reversal_risk")}, "holding_window": alert.get("current_holding_window"), "timeframe_agreement": alert.get("timeframe_agreement") or {}, "model_reference": {"timeframe": "1H", "candle_timestamp": reference_start, "candle_state": "CLOSED", "information_time": candle.end.isoformat()}}
            snapshot_id = self.record_alert(alert=alert, reference_price=float(candle.close), state=state)
            completed += self.complete_snapshot(snapshot_id, reference_time=candle.end, reference_price=float(candle.close), direction=state.get("direction"), candles=outcome_candles)
            recorded += 1
        return {"recorded_snapshots": recorded, "outcomes_written": completed}

    def summary(self, *, window: str | None = None, as_of: datetime | None = None, **filters: Any) -> dict[str, Any]:
        as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
        filters["window_start"] = window_start(as_of, window or self.config.default_window).isoformat()
        rows = self.database.research_summary(**filters)
        snapshot_filters = {key: value for key, value in filters.items() if key != "horizon"}
        snapshot_count = len(self.database.get_research_snapshots(**snapshot_filters))
        complete_snapshot_count = self.database.completed_research_snapshot_count(**snapshot_filters)
        outcome_count = sum(row["sample_count"] for row in rows)
        return {
            "rows": rows,
            "sample_count": complete_snapshot_count,
            "snapshot_count": snapshot_count,
            "outcome_observation_count": outcome_count,
            "quality": sample_quality(complete_snapshot_count, self.config),
            "research_version": self.config.version,
            "as_of": as_of.isoformat(),
            "labels": {
                "research": DESCRIPTIVE_RESEARCH_LABEL,
                "probability": NOT_CALIBRATED_PROBABILITY_LABEL,
                "performance": NOT_OUT_OF_SAMPLE_LABEL,
            },
        }
