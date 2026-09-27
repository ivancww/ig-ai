from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Candle, Instrument, MarketObservation, as_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (instrument_id TEXT PRIMARY KEY, epic TEXT NOT NULL UNIQUE, market_name TEXT NOT NULL, instrument_type TEXT, market_status TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (instrument_id TEXT NOT NULL, observed_at TEXT NOT NULL, epic TEXT NOT NULL, market_name TEXT NOT NULL, bid TEXT, offer TEXT, mid TEXT, market_state TEXT, source TEXT NOT NULL, PRIMARY KEY (instrument_id, observed_at));
CREATE TABLE IF NOT EXISTS candles (instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, epic TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT, is_closed INTEGER NOT NULL, observation_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (instrument_id, timeframe, start_at));
CREATE INDEX IF NOT EXISTS observations_instrument_time ON observations (instrument_id, observed_at);
CREATE INDEX IF NOT EXISTS candles_instrument_time ON candles (instrument_id, timeframe, start_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        version = self.connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        if version < 2:
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(candles)")}
            if "observation_count" not in columns:
                self.connection.execute("ALTER TABLE candles ADD COLUMN observation_count INTEGER NOT NULL DEFAULT 0")
            self.connection.execute("DELETE FROM schema_version")
            self.connection.execute("INSERT INTO schema_version VALUES (2)")
        elif version == 0:
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

    def save_instrument_model(self, instrument: Instrument) -> None:
        self.save_instrument(
            instrument.instrument_id, instrument.epic, instrument.market_name,
            instrument_type=instrument.instrument_type, market_status=instrument.market_status,
            metadata=instrument.metadata,
        )

    def list_instruments(self) -> list[tuple]:
        return self.connection.execute(
            "SELECT instrument_id, epic, market_name, instrument_type, market_status, metadata_json FROM instruments ORDER BY market_name"
        ).fetchall()

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
            "INSERT OR REPLACE INTO candles (instrument_id, timeframe, start_at, end_at, epic, open, high, low, close, volume, is_closed, observation_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                candle.observation_count,
            ),
        )
        self.connection.commit()
