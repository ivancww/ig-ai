"""Deterministic historical outcome and research foundation.

Research snapshots are point-in-time records.  Future candles are accepted
only by :class:`OutcomeCalculator` when completing outcomes, never while
constructing technical state.  All percentages are descriptive sample
statistics with an explicit sample count; they are not probabilities.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Candle

RESEARCH_ENGINE_VERSION = "research_engine_v1"
RESEARCH_SOURCE = "ig_cfd"
HORIZON_MINUTES = {"15M": 15, "1H": 60, "2H": 120, "4H": 240, "8H": 480, "1D": 1440}
RESEARCH_WINDOWS_YEARS = (1, 3, 5)
SCORE_BUCKETS = ((50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 100))


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


def regime_tags(snapshot: dict[str, Any], *, volatility_comparison: str | None = None) -> dict[str, str]:
    direction = snapshot.get("direction")
    stage = snapshot.get("trend_stage")
    if direction == "UP" and stage not in {"EARLY", "REVERSAL_WATCH", "REVERSAL_CONFIRMED"}:
        trend = "TRENDING_UP"
    elif direction == "DOWN" and stage not in {"EARLY", "REVERSAL_WATCH", "REVERSAL_CONFIRMED"}:
        trend = "TRENDING_DOWN"
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


def _candle_at_or_after(candles: list[Candle], target: datetime) -> Candle | None:
    return next((candle for candle in candles if candle.is_closed and candle.start >= target), None)


class OutcomeCalculator:
    """Calculate outcomes only from candles after an already recorded snapshot."""

    def __init__(self, config: ResearchConfig | None = None):
        self.config = config or ResearchConfig()

    def calculate(self, *, reference_price: float, reference_time: datetime, direction: str | None, candles: list[Candle], horizon: str) -> dict[str, Any]:
        if horizon not in HORIZON_MINUTES:
            raise ValueError(f"unsupported horizon: {horizon}")
        target = reference_time.astimezone(UTC) + timedelta(minutes=HORIZON_MINUTES[horizon])
        eligible = [candle for candle in candles if candle.is_closed and candle.start > reference_time and candle.start <= target]
        future = _candle_at_or_after(candles, target)
        if future is None:
            return {"status": "PENDING", "future_timestamp": None, "future_price": None, "absolute_move": None, "percentage_move": None, "direction_outcome": None, "mfe": None, "mae": None, "time_to_mfe": None, "time_to_mae": None, "time_to_reversal": None, "original_direction_valid": None}
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
        valid = None if direction not in {"UP", "DOWN"} else all(
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
            "status": "COMPLETE", "future_timestamp": future.start.isoformat(), "future_price": future_price,
            "absolute_move": future_price - reference_price, "percentage_move": (future_price - reference_price) / reference_price * 100 if reference_price else None,
            "direction_outcome": _direction_outcome(reference_price, future_price, self.config.flat_threshold),
            "mfe": mfe, "mae": mae, "time_to_mfe": (mfe_candle.start - reference_time).total_seconds() if mfe_candle else None,
            "time_to_mae": (mae_candle.start - reference_time).total_seconds() if mae_candle else None,
            "time_to_reversal": (reversal.start - reference_time).total_seconds() if reversal else None,
            "continuation_duration": (reversal.start - reference_time).total_seconds() if reversal else (future.start - reference_time).total_seconds(),
            "original_direction_valid": valid,
        }


class ResearchEngine:
    def __init__(self, database, config: ResearchConfig | None = None):
        self.database = database
        self.config = config or ResearchConfig()
        self.calculator = OutcomeCalculator(self.config)

    def record_snapshot(self, *, instrument_id: str, timeframe: str, snapshot_timestamp: str, reference_price: float, snapshot_type: str, state: dict[str, Any], source_identity: str = RESEARCH_SOURCE, pattern_name: str | None = None, pattern_lifecycle: str | None = None, alert_type: str | None = None, volatility_comparison: str | None = None) -> str:
        direction = state.get("direction")
        snapshot_id = hashlib.sha256(f"{self.config.version}:{instrument_id}:{timeframe}:{snapshot_type}:{snapshot_timestamp}:{pattern_name or ''}:{alert_type or ''}".encode()).hexdigest()
        regimes = regime_tags(state, volatility_comparison=volatility_comparison)
        agreement = state.get("timeframe_agreement") or {}
        payload = dict(state)
        payload["research_engine_version"] = self.config.version
        self.database.save_research_snapshot({"snapshot_id": snapshot_id, "instrument_id": instrument_id, "timeframe": timeframe, "snapshot_timestamp": snapshot_timestamp, "snapshot_type": snapshot_type, "source_identity": source_identity, "reference_price": reference_price, "direction": direction, "up_score": state.get("up_score"), "down_score": state.get("down_score"), "coverage": state.get("coverage"), "trend_stage": state.get("trend_stage"), "reversal_risk": (state.get("reversal_risk") or {}).get("category"), "holding_window": state.get("holding_window"), "agreement": agreement.get("status"), "four_hour_alignment": (agreement.get("states") or {}).get("4H"), "pattern_name": pattern_name, "pattern_lifecycle": pattern_lifecycle, "alert_type": alert_type, "score_bucket": score_bucket(state.get("up_score") if direction == "UP" else state.get("down_score")), "trend_regime": regimes["trend_regime"], "volatility_regime": regimes["volatility_regime"], "context_json": json.dumps(payload, sort_keys=True, separators=(",", ":")), "feature_version": state.get("feature_schema_version"), "pattern_version": state.get("pattern_schema_version"), "direction_version": state.get("score_version"), "alert_version": state.get("alert_engine_version"), "research_version": self.config.version})
        return snapshot_id

    def complete_snapshot(self, snapshot_id: str, *, reference_time: datetime, reference_price: float, direction: str | None, candles: list[Candle], horizons: tuple[str, ...] = tuple(HORIZON_MINUTES)) -> int:
        count = 0
        for horizon in horizons:
            outcome = self.calculator.calculate(reference_price=reference_price, reference_time=reference_time, direction=direction, candles=candles, horizon=horizon)
            self.database.save_research_outcome(snapshot_id, horizon, outcome)
            count += 1
        return count

    def record_pattern(self, *, pattern: dict[str, Any], reference_price: float, state: dict[str, Any]) -> str:
        return self.record_snapshot(instrument_id=pattern["instrument"], timeframe=pattern["timeframe"], snapshot_timestamp=pattern.get("start") or pattern["candle_timestamp"], reference_price=reference_price, snapshot_type="PATTERN", state=state, pattern_name=pattern.get("pattern"), pattern_lifecycle=pattern.get("lifecycle"), volatility_comparison=(state.get("context") or {}).get("atr", {}).get("recent_comparison"))

    def record_alert(self, *, alert: dict[str, Any], reference_price: float, state: dict[str, Any]) -> str:
        return self.record_snapshot(instrument_id=alert["instrument"], timeframe=(state.get("model_reference") or {}).get("timeframe", "1H"), snapshot_timestamp=alert.get("model_reference_time") or alert["created_at"], reference_price=reference_price, snapshot_type="ALERT", state=state, alert_type=alert.get("alert_type"))

    def run_direction_research(self, *, instrument_id: str, window: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        window = window or self.config.default_window
        now = now or datetime.now(UTC)
        start = window_start(now, window)
        rows = self.database.get_direction_snapshots(instrument_id, start.isoformat())
        recorded = 0
        completed = 0
        for state in rows:
            reference_time = datetime.fromisoformat(state["candle_timestamp"])
            # Use the finest available existing CFD candles to measure every
            # requested horizon without inventing intrabar prices.
            candles = self.database.list_candles(instrument_id, "15M", through=None)
            if reference_time < start or state.get("candle_state") != "CLOSED":
                continue
            features = self.database.get_technical_features(instrument_id, "1H", limit=100000)
            feature = next((item for item in features if item.get("candle_timestamp") == state["candle_timestamp"]), {})
            reference_price = (feature.get("candle") or {}).get("close")
            if reference_price is None:
                candle = next((item for item in candles if item.start == reference_time), None)
                reference_price = float(candle.close) if candle else None
            if reference_price is None:
                continue
            snapshot_id = self.record_snapshot(instrument_id=instrument_id, timeframe="1H", snapshot_timestamp=state["candle_timestamp"], reference_price=reference_price, snapshot_type="DIRECTION", state=state, volatility_comparison=(feature.get("atr14") or {}).get("recent_comparison"))
            completed += self.complete_snapshot(snapshot_id, reference_time=reference_time, reference_price=float(reference_price), direction=state.get("direction"), candles=candles)
            recorded += 1
        available = self.database.available_history(instrument_id, "1H", requested_window=window)
        return {"window": window, "requested_start": start.isoformat(), "recorded_snapshots": recorded, "outcomes_written": completed, "available_history": available}

    def summary(self, *, window: str | None = None, **filters: Any) -> dict[str, Any]:
        filters["window_start"] = window_start(datetime.now(UTC), window or self.config.default_window).isoformat()
        rows = self.database.research_summary(**filters)
        sample_count = sum(row["sample_count"] for row in rows)
        return {"rows": rows, "sample_count": sample_count, "quality": sample_quality(sample_count, self.config), "research_version": self.config.version}
