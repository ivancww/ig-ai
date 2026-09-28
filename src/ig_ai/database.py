from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .alerts import ALERT_ENGINE_VERSION
from .direction import DIRECTION_SCHEMA_VERSION
from .models import Candle, Instrument, MarketObservation, as_utc
from .patterns import PATTERN_SCHEMA_VERSION
from .technical import FEATURE_SCHEMA_VERSION, TechnicalFeatureEngine

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (instrument_id TEXT PRIMARY KEY, epic TEXT NOT NULL UNIQUE, market_name TEXT NOT NULL, instrument_type TEXT, market_status TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (instrument_id TEXT NOT NULL, observed_at TEXT NOT NULL, epic TEXT NOT NULL, market_name TEXT NOT NULL, bid TEXT, offer TEXT, mid TEXT, market_state TEXT, source TEXT NOT NULL, PRIMARY KEY (instrument_id, observed_at));
CREATE TABLE IF NOT EXISTS candles (instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, epic TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT, is_closed INTEGER NOT NULL, observation_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (instrument_id, timeframe, start_at));
CREATE INDEX IF NOT EXISTS observations_instrument_time ON observations (instrument_id, observed_at);
CREATE INDEX IF NOT EXISTS candles_instrument_time ON candles (instrument_id, timeframe, start_at);
CREATE TABLE IF NOT EXISTS technical_features (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    features_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, feature_schema_version)
);
CREATE INDEX IF NOT EXISTS technical_features_lookup ON technical_features (instrument_id, timeframe, candle_start);
CREATE TABLE IF NOT EXISTS technical_outcomes (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    feature_candle_start TEXT NOT NULL,
    horizon TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    future_timestamp TEXT,
    future_price TEXT,
    absolute_move TEXT,
    percentage_move TEXT,
    max_favourable_move TEXT,
    max_adverse_move TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, feature_candle_start, horizon)
);
CREATE TABLE IF NOT EXISTS pattern_observations (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    pattern_instance_id TEXT NOT NULL,
    pattern_schema_version TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, pattern_instance_id, candle_start, pattern_schema_version)
);
CREATE TABLE IF NOT EXISTS pattern_current (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    pattern_instance_id TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    pattern_schema_version TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, pattern_instance_id, pattern_schema_version)
);
CREATE TABLE IF NOT EXISTS structure_states (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    pattern_schema_version TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, pattern_schema_version)
);
CREATE TABLE IF NOT EXISTS direction_evidence (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    pattern_schema_version TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, pattern_schema_version)
);
CREATE TABLE IF NOT EXISTS pattern_outcomes (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    pattern_start TEXT NOT NULL,
    horizon TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    future_timestamp TEXT,
    future_price TEXT,
    absolute_move TEXT,
    percentage_move TEXT,
    max_favourable_move TEXT,
    max_adverse_move TEXT,
    time_to_reversal TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, pattern_name, pattern_start, horizon)
);
CREATE TABLE IF NOT EXISTS direction_snapshots (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    direction_schema_version TEXT NOT NULL,
    is_closed INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, direction_schema_version)
);
CREATE TABLE IF NOT EXISTS direction_outcomes (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    snapshot_candle_start TEXT NOT NULL,
    horizon TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    future_timestamp TEXT,
    future_price TEXT,
    absolute_move TEXT,
    percentage_move TEXT,
    max_favourable_move TEXT,
    max_adverse_move TEXT,
    time_to_reversal TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, snapshot_candle_start, horizon)
);
CREATE TABLE IF NOT EXISTS monitor_state (
    instrument_id TEXT NOT NULL,
    alert_engine_version TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, alert_engine_version)
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    event_identity TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    model_reference_time TEXT,
    trigger_time TEXT,
    previous_direction TEXT,
    current_direction TEXT,
    previous_score REAL,
    current_score REAL,
    previous_trend_stage TEXT,
    current_trend_stage TEXT,
    previous_reversal_risk TEXT,
    current_reversal_risk TEXT,
    previous_holding_window TEXT,
    current_holding_window TEXT,
    timeframe_agreement_json TEXT,
    trigger_evidence_json TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    score_version TEXT,
    alert_engine_version TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_instrument_time ON alerts (instrument_id, created_at);
CREATE TABLE IF NOT EXISTS alert_delivery_state (
    alert_id TEXT PRIMARY KEY,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    delivered_at TEXT,
    error_code TEXT,
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        pattern_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(pattern_observations)")}
        if pattern_columns and "pattern_instance_id" not in pattern_columns:
            self.connection.execute("BEGIN")
            try:
                self.connection.execute("ALTER TABLE pattern_observations RENAME TO pattern_observations_legacy")
                self.connection.execute("""CREATE TABLE pattern_observations (
                    instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, candle_start TEXT NOT NULL,
                    pattern_name TEXT NOT NULL, pattern_instance_id TEXT NOT NULL,
                    pattern_schema_version TEXT NOT NULL, lifecycle TEXT NOT NULL, is_closed INTEGER NOT NULL,
                    observation_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (instrument_id, timeframe, pattern_instance_id, candle_start, pattern_schema_version)
                )""")
                self.connection.execute(
                    "INSERT INTO pattern_observations SELECT instrument_id, timeframe, candle_start, pattern_name, pattern_name || ':' || candle_start, pattern_schema_version, lifecycle, is_closed, observation_json, updated_at FROM pattern_observations_legacy"
                )
                self.connection.execute("DROP TABLE pattern_observations_legacy")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
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

    def save_observation(self, observation: MarketObservation) -> bool:
        timestamp = as_utc(observation.timestamp).isoformat()
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        return cursor.rowcount == 1

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

    def list_candles(self, instrument_id: str, timeframe: str, *, through: str | None = None, limit: int | None = None) -> list[Candle]:
        query = (
            "SELECT instrument_id, timeframe, start_at, end_at, epic, open, high, low, close, volume, is_closed, observation_count "
            "FROM candles WHERE instrument_id = ? AND timeframe = ?"
        )
        params: list[str] = [instrument_id, timeframe]
        if through is not None:
            query += " AND start_at <= ?"
            params.append(through)
        if limit is not None:
            if limit <= 0:
                return []
            query += " ORDER BY start_at DESC LIMIT ?"
            params.append(str(limit))
            rows = list(reversed(self.connection.execute(query, params).fetchall()))
        else:
            query += " ORDER BY start_at"
            rows = self.connection.execute(query, params).fetchall()
        return [
            Candle(row[0], row[4], row[1], datetime.fromisoformat(row[2]), datetime.fromisoformat(row[3]), Decimal(row[5]), Decimal(row[6]), Decimal(row[7]), Decimal(row[8]), Decimal(row[9]) if row[9] is not None else None, bool(row[10]), int(row[11]))
            for row in rows
        ]

    def save_technical_features(self, features: dict) -> None:
        self.connection.execute(
            "INSERT INTO technical_features (instrument_id, timeframe, candle_start, feature_schema_version, is_closed, features_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instrument_id, timeframe, candle_start, feature_schema_version) DO UPDATE SET is_closed=excluded.is_closed, features_json=excluded.features_json, updated_at=excluded.updated_at",
            (features["instrument"], features["timeframe"], features["candle_timestamp"], features["schema_version"], int(features["candle_state"] == "CLOSED"), json.dumps(features, sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def save_features_for_candle(self, candle: Candle, engine: TechnicalFeatureEngine | None = None) -> dict:
        history = self.list_candles(candle.instrument_id, candle.timeframe, through=candle.start.isoformat())
        if not history or history[-1].start != candle.start:
            history.append(candle)
        features = (engine or TechnicalFeatureEngine()).calculate(history)
        self.save_technical_features(features)
        return features

    def get_technical_features(self, instrument_id: str, timeframe: str, *, limit: int = 1) -> list[dict]:
        rows = self.connection.execute(
            "SELECT features_json FROM technical_features WHERE instrument_id=? AND timeframe=? AND feature_schema_version=? ORDER BY candle_start DESC LIMIT ?",
            (instrument_id, timeframe, FEATURE_SCHEMA_VERSION, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_outcome(self, *, instrument_id: str, timeframe: str, feature_candle_start: str, horizon: str, reference_price: str, future_timestamp: str | None = None, future_price: str | None = None, absolute_move: str | None = None, percentage_move: str | None = None, max_favourable_move: str | None = None, max_adverse_move: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO technical_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, feature_candle_start, horizon) DO UPDATE SET future_timestamp=excluded.future_timestamp, future_price=excluded.future_price, absolute_move=excluded.absolute_move, percentage_move=excluded.percentage_move, max_favourable_move=excluded.max_favourable_move, max_adverse_move=excluded.max_adverse_move",
            (instrument_id, timeframe, feature_candle_start, horizon, reference_price, future_timestamp, future_price, absolute_move, percentage_move, max_favourable_move, max_adverse_move, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def save_phase2b_analysis(self, analysis: dict) -> None:
        instrument = analysis["instrument"]
        timeframe = analysis["timeframe"]
        candle_start = analysis["candle_timestamp"]
        is_closed = int(analysis["candle_state"] == "CLOSED")
        now = datetime.now(UTC).isoformat()
        for observation in analysis.get("candlestick_patterns", []) + analysis.get("chart_patterns", []) + analysis.get("divergences", []):
            name = observation["pattern"]
            instance_id = observation.get("instance_id") or f"{name}:{observation['start']}"
            self.connection.execute(
                "INSERT INTO pattern_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, pattern_instance_id, candle_start, pattern_schema_version) DO UPDATE SET lifecycle=excluded.lifecycle, is_closed=excluded.is_closed, observation_json=excluded.observation_json, updated_at=excluded.updated_at",
                (instrument, timeframe, candle_start, name, instance_id, PATTERN_SCHEMA_VERSION, observation.get("lifecycle", "FORMING"), is_closed, json.dumps(observation, sort_keys=True, separators=(",", ":")), now),
            )
            existing = self.connection.execute(
                "SELECT lifecycle FROM pattern_current WHERE instrument_id=? AND timeframe=? AND pattern_instance_id=? AND pattern_schema_version=?",
                (instrument, timeframe, instance_id, PATTERN_SCHEMA_VERSION),
            ).fetchone()
            lifecycle = observation.get("lifecycle", "FORMING")
            if not existing or existing[0] not in {"CONFIRMED", "FAILED"} or lifecycle in {"CONFIRMED", "FAILED"}:
                self.connection.execute(
                    "INSERT INTO pattern_current VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, pattern_instance_id, pattern_schema_version) DO UPDATE SET candle_start=excluded.candle_start, lifecycle=excluded.lifecycle, is_closed=excluded.is_closed, observation_json=excluded.observation_json, updated_at=excluded.updated_at",
                    (instrument, timeframe, instance_id, name, PATTERN_SCHEMA_VERSION, candle_start, lifecycle, is_closed, json.dumps(observation, sort_keys=True, separators=(",", ":")), now),
                )
        structure = analysis.get("structure", {})
        self.connection.execute(
            "INSERT INTO structure_states VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, candle_start, pattern_schema_version) DO UPDATE SET is_closed=excluded.is_closed, state_json=excluded.state_json, updated_at=excluded.updated_at",
            (instrument, timeframe, candle_start, PATTERN_SCHEMA_VERSION, is_closed, json.dumps(structure, sort_keys=True, separators=(",", ":")), now),
        )
        direction = analysis.get("direction_reversal")
        if direction is not None:
            self.connection.execute(
                "INSERT INTO direction_evidence VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, candle_start, pattern_schema_version) DO UPDATE SET is_closed=excluded.is_closed, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
                (instrument, timeframe, candle_start, PATTERN_SCHEMA_VERSION, is_closed, json.dumps(direction, sort_keys=True, separators=(",", ":")), now),
            )
        self.connection.commit()

    def get_phase2b_status(self, instrument_id: str, timeframe: str) -> dict:
        structure = self.connection.execute(
            "SELECT state_json FROM structure_states WHERE instrument_id=? AND timeframe=? AND pattern_schema_version=? ORDER BY candle_start DESC LIMIT 1",
            (instrument_id, timeframe, PATTERN_SCHEMA_VERSION),
        ).fetchone()
        patterns = self.connection.execute(
            "SELECT observation_json FROM pattern_current WHERE instrument_id=? AND timeframe=? AND pattern_schema_version=? ORDER BY candle_start DESC, pattern_name LIMIT 50",
            (instrument_id, timeframe, PATTERN_SCHEMA_VERSION),
        ).fetchall()
        direction = self.connection.execute(
            "SELECT evidence_json FROM direction_evidence WHERE instrument_id=? AND timeframe=? AND pattern_schema_version=? ORDER BY candle_start DESC LIMIT 1",
            (instrument_id, timeframe, PATTERN_SCHEMA_VERSION),
        ).fetchone()
        return {"structure": json.loads(structure[0]) if structure else None, "patterns": [json.loads(row[0]) for row in patterns], "direction_reversal": json.loads(direction[0]) if direction else None}

    def save_direction_snapshot(self, snapshot: dict) -> None:
        self.connection.execute(
            "INSERT INTO direction_snapshots VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, candle_start, direction_schema_version) DO UPDATE SET is_closed=excluded.is_closed, snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
            (snapshot["instrument"], snapshot["timeframe"], snapshot["candle_timestamp"], snapshot.get("schema_version", DIRECTION_SCHEMA_VERSION), int(snapshot.get("candle_state") == "CLOSED"), json.dumps(snapshot, sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def get_direction_status(self, instrument_id: str, timeframe: str = "1H") -> dict | None:
        row = self.connection.execute(
            "SELECT snapshot_json FROM direction_snapshots WHERE instrument_id=? AND timeframe=? AND direction_schema_version=? ORDER BY candle_start DESC LIMIT 1",
            (instrument_id, timeframe, DIRECTION_SCHEMA_VERSION),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_direction_outcome(self, *, instrument_id: str, timeframe: str, snapshot_candle_start: str, horizon: str, reference_price: str, future_timestamp: str | None = None, future_price: str | None = None, absolute_move: str | None = None, percentage_move: str | None = None, max_favourable_move: str | None = None, max_adverse_move: str | None = None, time_to_reversal: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO direction_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, snapshot_candle_start, horizon) DO UPDATE SET future_timestamp=excluded.future_timestamp, future_price=excluded.future_price, absolute_move=excluded.absolute_move, percentage_move=excluded.percentage_move, max_favourable_move=excluded.max_favourable_move, max_adverse_move=excluded.max_adverse_move, time_to_reversal=excluded.time_to_reversal",
            (instrument_id, timeframe, snapshot_candle_start, horizon, reference_price, future_timestamp, future_price, absolute_move, percentage_move, max_favourable_move, max_adverse_move, time_to_reversal, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def get_monitor_state(self, instrument_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT state_json FROM monitor_state WHERE instrument_id=? AND alert_engine_version=?",
            (instrument_id, ALERT_ENGINE_VERSION),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_monitor_state(self, state: dict) -> None:
        self._save_monitor_state(self.connection, state)
        self.connection.commit()

    @staticmethod
    def _save_monitor_state(connection: sqlite3.Connection, state: dict) -> None:
        connection.execute(
            "INSERT INTO monitor_state VALUES (?, ?, ?, ?) ON CONFLICT(instrument_id, alert_engine_version) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (state["instrument"], state.get("schema_version", ALERT_ENGINE_VERSION), json.dumps(state, sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _insert_alert(connection: sqlite3.Connection, alert: dict) -> bool:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO alerts (alert_id, instrument_id, alert_type, priority, event_identity, created_at, model_reference_time, trigger_time, previous_direction, current_direction, previous_score, current_score, previous_trend_stage, current_trend_stage, previous_reversal_risk, current_reversal_risk, previous_holding_window, current_holding_window, timeframe_agreement_json, trigger_evidence_json, confirmed, score_version, alert_engine_version, message, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alert["alert_id"], alert["instrument"], alert["alert_type"], alert["priority"], alert["event_identity"], alert["created_at"], alert.get("model_reference_time"), alert.get("trigger_time"), alert.get("previous_direction"), alert.get("current_direction"), alert.get("previous_score"), alert.get("current_score"), alert.get("previous_trend_stage"), alert.get("current_trend_stage"), alert.get("previous_reversal_risk"), alert.get("current_reversal_risk"), alert.get("previous_holding_window"), alert.get("current_holding_window"), json.dumps(alert.get("timeframe_agreement"), sort_keys=True, separators=(",", ":")), json.dumps(alert.get("trigger_evidence", []), sort_keys=True, separators=(",", ":")), int(alert.get("confirmed", False)), alert.get("score_version"), alert.get("alert_engine_version", ALERT_ENGINE_VERSION), alert["message"], json.dumps(alert, sort_keys=True, separators=(",", ":"))),
        )
        connection.execute("INSERT OR IGNORE INTO alert_delivery_state (alert_id) VALUES (?)", (alert["alert_id"],))
        return cursor.rowcount == 1

    def save_alert(self, alert: dict) -> bool:
        with self.connection:
            return self._insert_alert(self.connection, alert)

    def save_monitor_evaluation(self, state: dict, alerts: list[dict]) -> int:
        """Atomically accept monitor state and all alerts from that evaluation."""
        with self.connection:
            inserted = sum(self._insert_alert(self.connection, alert) for alert in alerts)
            self._save_monitor_state(self.connection, state)
        return inserted

    def list_alerts(self, instrument_id: str | None = None, priority: str | None = None, limit: int = 20) -> list[dict]:
        query = "SELECT payload_json FROM alerts WHERE 1=1"
        params: list[object] = []
        if instrument_id:
            query += " AND instrument_id=?"
            params.append(instrument_id)
        if priority:
            query += " AND priority=?"
            params.append(priority)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [json.loads(row[0]) for row in self.connection.execute(query, params).fetchall()]

    def get_monitor_status(self, instrument_id: str | None = None) -> dict:
        if instrument_id:
            states = self.connection.execute("SELECT state_json FROM monitor_state WHERE instrument_id=? ORDER BY updated_at DESC", (instrument_id,)).fetchall()
            count = self.connection.execute("SELECT COUNT(*) FROM alerts WHERE instrument_id=?", (instrument_id,)).fetchone()[0]
            last = self.connection.execute("SELECT payload_json FROM alerts WHERE instrument_id=? ORDER BY created_at DESC LIMIT 1", (instrument_id,)).fetchone()
        else:
            states = self.connection.execute("SELECT state_json FROM monitor_state ORDER BY updated_at DESC").fetchall()
            count = self.connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            last = self.connection.execute("SELECT payload_json FROM alerts ORDER BY created_at DESC LIMIT 1").fetchone()
        return {"states": [json.loads(row[0]) for row in states], "alert_count": count, "last_alert": json.loads(last[0]) if last else None}

    def save_pattern_outcome(self, *, instrument_id: str, timeframe: str, pattern_name: str, pattern_start: str, horizon: str, reference_price: str, future_timestamp: str | None = None, future_price: str | None = None, absolute_move: str | None = None, percentage_move: str | None = None, max_favourable_move: str | None = None, max_adverse_move: str | None = None, time_to_reversal: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO pattern_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument_id, timeframe, pattern_name, pattern_start, horizon) DO UPDATE SET future_timestamp=excluded.future_timestamp, future_price=excluded.future_price, absolute_move=excluded.absolute_move, percentage_move=excluded.percentage_move, max_favourable_move=excluded.max_favourable_move, max_adverse_move=excluded.max_adverse_move, time_to_reversal=excluded.time_to_reversal",
            (instrument_id, timeframe, pattern_name, pattern_start, horizon, reference_price, future_timestamp, future_price, absolute_move, percentage_move, max_favourable_move, max_adverse_move, time_to_reversal, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def load_forming_candles(self) -> list[Candle]:
        rows = self.connection.execute(
            "SELECT instrument_id, timeframe, start_at, end_at, epic, open, high, low, close, volume, is_closed, observation_count "
            "FROM candles WHERE is_closed = 0 ORDER BY instrument_id, timeframe, start_at"
        ).fetchall()
        return [
            Candle(
                instrument_id=row[0], timeframe=row[1],
                start=datetime.fromisoformat(row[2]), end=datetime.fromisoformat(row[3]),
                epic=row[4], open=Decimal(row[5]), high=Decimal(row[6]),
                low=Decimal(row[7]), close=Decimal(row[8]),
                volume=Decimal(row[9]) if row[9] is not None else None,
                is_closed=bool(row[10]), observation_count=int(row[11]),
            )
            for row in rows
        ]
