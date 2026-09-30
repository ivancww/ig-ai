"""Immutable LIVE_FORWARD snapshots and closed-candle outcome tracking."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .research import OutcomeCalculator

FORWARD_PROVENANCE = "LIVE_FORWARD"
FORWARD_MODEL_VERSION = "step8a_forward_v1"
FORWARD_HORIZONS = ("15M", "1H", "2H", "4H", "8H", "1D")


def source_identity(*, instrument_id: str, epic: str, market: str) -> str:
    return f"{FORWARD_PROVENANCE}|provider=IG|source=STREAM|asset=CFD|instrument={instrument_id}|epic={epic}|market={market}"


class ForwardTestEngine:
    """Record point-in-time live states without rewriting their predictions."""

    def __init__(self, database: Any):
        self.database = database
        self.calculator = OutcomeCalculator()
        self.last_inserted = False

    def record_snapshot(self, *, reference_candle: Any, coordinated: dict[str, Any]) -> str:
        if not reference_candle.is_closed or reference_candle.timeframe != "1H":
            raise ValueError("LIVE_FORWARD snapshots require a closed 1H candle")
        direction = dict(coordinated.get("direction_score") or {})
        primary = dict(coordinated.get("timeframes", {}).get("1H") or {})
        early = dict(coordinated.get("timeframes", {}).get("15M") or {})
        snapshot_id = hashlib.sha256(
            f"{FORWARD_PROVENANCE}:{reference_candle.instrument_id}:1H:{reference_candle.end.isoformat()}:{FORWARD_MODEL_VERSION}".encode()
        ).hexdigest()
        payload = {
            "technical_state": primary.get("context", {}),
            "pattern_state": {
                "primary_1H": primary.get("candlestick_patterns", []) + primary.get("chart_patterns", []),
                "early_15M": early.get("candlestick_patterns", []) + early.get("chart_patterns", []),
                "divergences_15M": early.get("divergences", []),
            },
            "direction_state": direction,
        }
        market = next((row[2] for row in self.database.list_instruments() if row[0] == reference_candle.instrument_id), reference_candle.epic)
        self.last_inserted = self.database.save_forward_snapshot({
            "snapshot_id": snapshot_id,
            "timestamp": reference_candle.end.isoformat(),
            "reference_time": reference_candle.end.isoformat(),
            "market": market,
            "instrument_id": reference_candle.instrument_id,
            "epic": reference_candle.epic,
            "source_identity": source_identity(instrument_id=reference_candle.instrument_id, epic=reference_candle.epic, market=market),
            "timeframe": "1H",
            "technical_state": payload["technical_state"],
            "pattern_state": payload["pattern_state"],
            "direction_state": payload["direction_state"],
            "reference_price": float(reference_candle.close),
            "direction": direction.get("direction"),
            "up_score": direction.get("up_score"),
            "down_score": direction.get("down_score"),
            "trend_stage": direction.get("trend_stage"),
            "reversal_state": direction.get("reversal_risk"),
            "holding_window": direction.get("holding_window"),
            "model_version": FORWARD_MODEL_VERSION,
        })
        self.database.save_forward_outcomes(
            snapshot_id,
            {horizon: {"status": "PENDING"} for horizon in FORWARD_HORIZONS},
        )
        return snapshot_id

    def update_outcomes(self, *, instrument_id: str, candles: list[Any]) -> int:
        closed = [candle for candle in candles if candle.is_closed]
        updated = 0
        for snapshot in self.database.pending_forward_snapshots(instrument_id):
            reference_time = datetime.fromisoformat(snapshot["reference_time"]).astimezone(UTC)
            reference_price = float(snapshot["reference_price"])
            direction = snapshot.get("direction")
            outcomes = {
                horizon: self.calculator.calculate(
                    reference_price=reference_price,
                    reference_time=reference_time,
                    direction=direction,
                    candles=closed,
                    horizon=horizon,
                )
                for horizon in FORWARD_HORIZONS
            }
            self.database.save_forward_outcomes(snapshot["snapshot_id"], outcomes)
            updated += sum(item.get("status") == "COMPLETE" for item in outcomes.values())
        return updated
