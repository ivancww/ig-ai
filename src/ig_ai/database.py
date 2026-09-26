from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Candle, MarketObservation, as_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (instrument_id TEXT PRIMARY KEY, epic TEXT NOT NULL UNIQUE, market_name TEXT NOT NULL, instrument_type TEXT, market_status TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (instrument_id TEXT NOT NULL, observed_at TEXT NOT NULL, epic TEXT NOT NULL, market_name TEXT NOT NULL, bid TEXT, offer TEXT, mid TEXT, market_state TEXT, source TEXT NOT NULL, PRIMARY KEY (instrument_id, observed_at));
CREATE TABLE IF NOT EXISTS candles (instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, epic TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT, is_closed INTEGER NOT NULL, PRIMARY KEY (instrument_id, timeframe, start_at));
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        if self.connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            self.connection.execute("INSERT INTO schema_version VALUES (1)")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_instrument(
        self,
        instrument_id: str,
        epic: str,
        market_name: str,
        *,
        instrument_type: str | None = None,
        market_status: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                instrument_id,
                epic,
                market_name,
                instrument_type,
                market_status,
                json.dumps(metadata or {}, sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def save_observation(self, observation: MarketObservation) -> None:
        timestamp = as_utc(observation.timestamp).isoformat()
        self.connection.execute(
            "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation.instrument_id,
                timestamp,
                observation.epic,
                observation.market_name,
                str(observation.bid) if observation.bid is not None else None,
                str(observation.offer) if observation.offer is not None else None,
                str(observation.mid) if observation.mid is not None else None,
                observation.market_state,
                observation.source,
            ),
        )
        self.connection.commit()

    def save_candle(self, candle: Candle) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candle.instrument_id,
                candle.timeframe,
                candle.start.isoformat(),
                candle.end.isoformat(),
                candle.epic,
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
                str(candle.volume) if candle.volume is not None else None,
                int(candle.is_closed),
            ),
        )
        self.connection.commit()
