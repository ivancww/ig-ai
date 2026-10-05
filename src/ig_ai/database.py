from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from statistics import median

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
CREATE TABLE IF NOT EXISTS alert_ui_state (
    alert_id TEXT PRIMARY KEY,
    read_at TEXT,
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE TABLE IF NOT EXISTS research_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    reference_time TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    reference_price REAL NOT NULL,
    direction TEXT,
    up_score REAL,
    down_score REAL,
    coverage_json TEXT,
    trend_stage TEXT,
    reversal_risk TEXT,
    holding_window TEXT,
    agreement TEXT,
    four_hour_alignment TEXT,
    pattern_name TEXT,
    pattern_lifecycle TEXT,
    instance_identity TEXT,
    divergence_indicator TEXT,
    divergence_direction TEXT,
    alert_type TEXT,
    alert_id TEXT,
    alert_event_identity TEXT,
    score_bucket TEXT,
    trend_regime TEXT NOT NULL,
    volatility_regime TEXT NOT NULL,
    context_json TEXT NOT NULL,
    feature_version TEXT,
    pattern_version TEXT,
    direction_version TEXT,
    alert_version TEXT,
    research_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (instrument_id, timeframe, snapshot_timestamp, snapshot_type, pattern_name, pattern_lifecycle, instance_identity, alert_type, alert_id, alert_event_identity, research_version)
);
CREATE INDEX IF NOT EXISTS research_snapshots_query ON research_snapshots (instrument_id, timeframe, snapshot_timestamp, snapshot_type);
CREATE TABLE IF NOT EXISTS research_outcomes (
    snapshot_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    status TEXT NOT NULL,
    future_timestamp TEXT,
    future_price REAL,
    absolute_move REAL,
    percentage_move REAL,
    direction_outcome TEXT,
    mfe REAL,
    mae REAL,
    time_to_mfe REAL,
    time_to_mae REAL,
    time_to_reversal REAL,
    continuation_duration REAL,
    direction_side_held INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, horizon),
    FOREIGN KEY (snapshot_id) REFERENCES research_snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS research_outcomes_query ON research_outcomes (horizon, status, direction_outcome);
CREATE TABLE IF NOT EXISTS research_regimes (
    snapshot_id TEXT PRIMARY KEY,
    trend_regime TEXT NOT NULL,
    volatility_regime TEXT NOT NULL,
    methodology_version TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES research_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS candle_provenance (
    instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, start_at TEXT NOT NULL,
    source TEXT NOT NULL, provenance_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, start_at)
);
CREATE INDEX IF NOT EXISTS candle_provenance_source ON candle_provenance (source, instrument_id, timeframe, start_at);
CREATE TABLE IF NOT EXISTS candle_quality (
    instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, start_at TEXT NOT NULL,
    eligibility TEXT NOT NULL, reason TEXT, recorded_at TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, start_at)
);
CREATE INDEX IF NOT EXISTS candle_quality_lookup ON candle_quality (instrument_id, timeframe, eligibility, start_at);
CREATE TABLE IF NOT EXISTS backfill_jobs (
    job_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, epic TEXT NOT NULL, market_name TEXT NOT NULL,
    timeframe TEXT NOT NULL, requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
    current_progress TEXT, last_successful_range TEXT, rows_retrieved INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0, rows_skipped INTEGER NOT NULL DEFAULT 0,
    malformed_rows INTEGER NOT NULL DEFAULT 0, skipped_malformed_rows INTEGER NOT NULL DEFAULT 0,
    malformed_diagnostics_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
    last_provider_metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS backfill_jobs_lookup ON backfill_jobs (instrument_id, timeframe, status, updated_at);
CREATE TABLE IF NOT EXISTS replay_jobs (
    job_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
    window TEXT NOT NULL, last_information_time TEXT, snapshots_generated INTEGER NOT NULL DEFAULT 0,
    outcomes_written INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS replay_jobs_lookup ON replay_jobs (instrument_id, status, updated_at);
CREATE TABLE IF NOT EXISTS forward_snapshots (
    snapshot_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, reference_time TEXT NOT NULL,
    market TEXT NOT NULL, instrument_id TEXT NOT NULL, epic TEXT NOT NULL,
    source_identity TEXT NOT NULL, provenance TEXT NOT NULL, timeframe TEXT NOT NULL,
    technical_state_json TEXT NOT NULL, pattern_state_json TEXT NOT NULL,
    direction_state_json TEXT NOT NULL, reference_price REAL NOT NULL,
    direction TEXT, up_score REAL, down_score REAL, trend_stage TEXT,
    reversal_state_json TEXT, holding_window TEXT, model_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (instrument_id, timeframe, reference_time, provenance, model_version)
);
CREATE INDEX IF NOT EXISTS forward_snapshots_lookup ON forward_snapshots (instrument_id, reference_time);
CREATE TABLE IF NOT EXISTS forward_snapshot_status (
    snapshot_id TEXT PRIMARY KEY, eligibility TEXT NOT NULL, reason TEXT,
    audited_at TEXT NOT NULL, FOREIGN KEY (snapshot_id) REFERENCES forward_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS forward_outcomes (
    snapshot_id TEXT NOT NULL, horizon TEXT NOT NULL, status TEXT NOT NULL,
    future_return REAL, future_timestamp TEXT, future_price REAL,
    directional_outcome TEXT, mfe REAL, mae REAL, continuation_timing REAL,
    reversal_timing REAL, updated_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, horizon), FOREIGN KEY (snapshot_id) REFERENCES forward_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS decision_telemetry (
    telemetry_id TEXT PRIMARY KEY,
    reference_time TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT,
    regime TEXT,
    primary_action_state TEXT NOT NULL,
    model_version TEXT NOT NULL,
    source_identity TEXT,
    telemetry_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_telemetry_lookup
ON decision_telemetry (instrument_id, reference_time, primary_action_state);
CREATE TABLE IF NOT EXISTS runtime_state (
    state_key TEXT PRIMARY KEY, state_value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_market_state (
    instrument_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_schedule (
    instrument_id TEXT PRIMARY KEY, mode TEXT NOT NULL, windows_json TEXT NOT NULL, timezone TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self._ensure_research_columns()
        self._ensure_backfill_columns()
        self._ensure_candle_quality()
        self._ensure_forward_snapshot_status()
        self.connection.commit()
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

    def _ensure_forward_snapshot_status(self) -> None:
        """Quarantine legacy snapshots that were created materially late.

        The original schema had no eligibility marker, so old rows are kept
        intact and classified from their immutable timestamps.  This is an
        audit classification only; no snapshot or outcome rows are deleted.
        """
        rows = self.connection.execute(
            "SELECT snapshot_id, reference_time, created_at FROM forward_snapshots"
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        for snapshot_id, reference_time, created_at in rows:
            existing = self.connection.execute(
                "SELECT 1 FROM forward_snapshot_status WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if existing:
                continue
            eligibility, reason = "ELIGIBLE", None
            try:
                reference = datetime.fromisoformat(reference_time).astimezone(UTC)
                created = datetime.fromisoformat(created_at).astimezone(UTC)
                if created - reference > timedelta(minutes=15):
                    eligibility, reason = "INVALID_STALE", "created materially after reference_time"
            except (TypeError, ValueError):
                eligibility, reason = "INVALID_STALE", "invalid snapshot timestamp"
            self.connection.execute(
                "INSERT INTO forward_snapshot_status (snapshot_id, eligibility, reason, audited_at) VALUES (?, ?, ?, ?)",
                (snapshot_id, eligibility, reason, now),
            )

    def _ensure_candle_quality(self) -> None:
        """Backfill neutral quality metadata without changing candle rows."""
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT OR IGNORE INTO candle_quality (instrument_id, timeframe, start_at, eligibility, reason, recorded_at) "
            "SELECT instrument_id, timeframe, start_at, 'ELIGIBLE', NULL, ? FROM candles",
            (now,),
        )

    def _ensure_research_columns(self) -> None:
        """Add non-destructive columns for databases created by an earlier 5A build."""
        migrations = {
            "research_snapshots": {
                "reference_time": "TEXT NOT NULL DEFAULT ''",
                "four_hour_alignment": "TEXT",
                "instance_identity": "TEXT",
                "divergence_indicator": "TEXT",
                "divergence_direction": "TEXT",
                "alert_id": "TEXT",
                "alert_event_identity": "TEXT",
            },
            "research_outcomes": {
                "continuation_duration": "REAL",
                "direction_side_held": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _ensure_backfill_columns(self) -> None:
        """Add non-destructive malformed-row accounting to existing jobs."""
        columns = {
            "malformed_rows": "INTEGER NOT NULL DEFAULT 0",
            "skipped_malformed_rows": "INTEGER NOT NULL DEFAULT 0",
            "malformed_diagnostics_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(backfill_jobs)")}
        for name, definition in columns.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE backfill_jobs ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.connection.close()

    def save_runtime_state(self, key: str, value: dict) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO runtime_state VALUES (?, ?, ?) ON CONFLICT(state_key) DO UPDATE SET state_value_json=excluded.state_value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value, sort_keys=True, separators=(",", ":")), now),
        )
        self.connection.commit()

    def get_runtime_state(self, key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT state_value_json FROM runtime_state WHERE state_key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_runtime_market_state(self, instrument_id: str, state: dict) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO runtime_market_state VALUES (?, ?, ?) ON CONFLICT(instrument_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (instrument_id, json.dumps(state, sort_keys=True, separators=(",", ":")), now),
        )
        self.connection.commit()

    def list_runtime_market_states(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT state_json FROM runtime_market_state ORDER BY instrument_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_runtime_schedule(self, instrument_id: str, schedule: dict) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "INSERT INTO runtime_schedule VALUES (?, ?, ?, ?, ?) ON CONFLICT(instrument_id) DO UPDATE SET mode=excluded.mode, windows_json=excluded.windows_json, timezone=excluded.timezone, updated_at=excluded.updated_at",
            (instrument_id, schedule["mode"], json.dumps(schedule.get("windows", [])), schedule["timezone"], now),
        )
        self.connection.commit()

    def list_runtime_schedules(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT instrument_id, mode, windows_json, timezone, updated_at FROM runtime_schedule ORDER BY instrument_id"
        ).fetchall()
        return [
            {"instrument_id": row[0], "mode": row[1], "windows": json.loads(row[2]), "timezone": row[3], "updated_at": row[4]}
            for row in rows
        ]

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
        existing_source = self.connection.execute(
            "SELECT source FROM candle_provenance WHERE instrument_id=? AND timeframe=? AND start_at=?",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat()),
        ).fetchone()
        if existing_source and existing_source[0] == "IG_HISTORICAL" and not candle.is_closed:
            return
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
        self.connection.execute(
            "INSERT OR IGNORE INTO candle_provenance VALUES (?, ?, ?, 'LIVE_AGGREGATED', ?, ?)",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat(), json.dumps({"provider": "IG", "asset_source_type": "CFD", "policy": "live_stream"}, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO candle_quality VALUES (?, ?, ?, 'ELIGIBLE', NULL, ?)",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat(), datetime.now(UTC).isoformat()),
        )
        if existing_source and existing_source[0] == "IG_HISTORICAL" and candle.is_closed:
            self.connection.execute(
                "UPDATE candle_provenance SET source='LIVE_AGGREGATED', provenance_json=?, recorded_at=? WHERE instrument_id=? AND timeframe=? AND start_at=?",
                (json.dumps({"provider": "IG", "asset_source_type": "CFD", "policy": "live_supersedes_historical"}, sort_keys=True), datetime.now(UTC).isoformat(), candle.instrument_id, candle.timeframe, candle.start.isoformat()),
            )
        self.connection.commit()

    @staticmethod
    def deterministic_backfill_job_id(instrument_id: str, timeframe: str, start: str, end: str) -> str:
        import hashlib
        return hashlib.sha256(f"backfill:{instrument_id}:{timeframe}:{start}:{end}".encode()).hexdigest()

    @staticmethod
    def deterministic_replay_job_id(instrument_id: str, start: str, end: str, window: str) -> str:
        import hashlib
        return hashlib.sha256(f"replay:{instrument_id}:{start}:{end}:{window}".encode()).hexdigest()

    def save_historical_candle(self, candle: Candle, *, provenance: dict) -> str:
        """Insert history without overwriting a live candle or its provenance."""
        identity = (candle.instrument_id, candle.timeframe, candle.start.isoformat())
        existing = self.connection.execute(
            "SELECT 1 FROM candles WHERE instrument_id=? AND timeframe=? AND start_at=?", identity
        ).fetchone()
        if existing:
            source = self.connection.execute(
                "SELECT source FROM candle_provenance WHERE instrument_id=? AND timeframe=? AND start_at=?", identity
            ).fetchone()
            return "SKIPPED_LIVE_CONFLICT" if source is None or source[0] == "LIVE_AGGREGATED" else "SKIPPED_DUPLICATE"
        self.connection.execute(
            "INSERT INTO candles (instrument_id, timeframe, start_at, end_at, epic, open, high, low, close, volume, is_closed, observation_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat(), candle.end.isoformat(), candle.epic, str(candle.open), str(candle.high), str(candle.low), str(candle.close), None, 1, 0),
        )
        sensitive = ("password", "api_key", "apikey", "cst", "security-token", "access_token", "refresh_token", "oauth", "lightstreamer")

        def scrub(value):
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items() if not any(token in str(key).lower() for token in sensitive)}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        self.connection.execute(
            "INSERT INTO candle_provenance VALUES (?, ?, ?, ?, ?, ?)",
            (*identity, "IG_HISTORICAL", json.dumps(scrub(provenance), sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat()),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO candle_quality VALUES (?, ?, ?, 'ELIGIBLE', NULL, ?)",
            (*identity, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return "INSERTED"

    def mark_candle_audit_only(self, candle: Candle, *, reason: str) -> None:
        """Preserve a candle while excluding it from analytical histories."""
        source = self.connection.execute(
            "SELECT source FROM candle_provenance WHERE instrument_id=? AND timeframe=? AND start_at=?",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat()),
        ).fetchone()
        if source and source[0] == "IG_HISTORICAL":
            return
        self.connection.execute(
            "INSERT INTO candle_quality (instrument_id, timeframe, start_at, eligibility, reason, recorded_at) VALUES (?, ?, ?, 'AUDIT_ONLY', ?, ?) "
            "ON CONFLICT(instrument_id, timeframe, start_at) DO UPDATE SET eligibility='AUDIT_ONLY', reason=excluded.reason, recorded_at=excluded.recorded_at",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat(), reason, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def is_candle_eligible(self, candle: Candle) -> bool:
        row = self.connection.execute(
            "SELECT eligibility FROM candle_quality WHERE instrument_id=? AND timeframe=? AND start_at=?",
            (candle.instrument_id, candle.timeframe, candle.start.isoformat()),
        ).fetchone()
        return row is None or row[0] == "ELIGIBLE"

    def start_backfill_job(self, job_id: str, instrument_id: str, epic: str, market_name: str, timeframe: str, requested_start: str, requested_end: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute("INSERT INTO backfill_jobs (job_id, instrument_id, epic, market_name, timeframe, requested_start, requested_end, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?) ON CONFLICT(job_id) DO UPDATE SET status='RUNNING', updated_at=excluded.updated_at", (job_id, instrument_id, epic, market_name, timeframe, requested_start, requested_end, now, now))
        self.connection.commit()

    def checkpoint_backfill_job(self, job_id: str, progress: str, retrieved: int, inserted: int, skipped: int, metadata: dict, malformed_rows: int = 0, skipped_malformed_rows: int = 0, malformed_diagnostics: list[dict] | None = None) -> None:
        self.connection.execute("UPDATE backfill_jobs SET current_progress=?, last_successful_range=?, rows_retrieved=?, rows_inserted=?, rows_skipped=?, malformed_rows=?, skipped_malformed_rows=?, malformed_diagnostics_json=?, last_provider_metadata_json=?, updated_at=? WHERE job_id=?", (progress, progress, retrieved, inserted, skipped, malformed_rows, skipped_malformed_rows, json.dumps(malformed_diagnostics or [], sort_keys=True, separators=(",", ":")), json.dumps(metadata, sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat(), job_id))
        self.connection.commit()

    def get_backfill_job(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT job_id, instrument_id, epic, market_name, timeframe, requested_start, requested_end, current_progress, last_successful_range, rows_retrieved, rows_inserted, rows_skipped, malformed_rows, skipped_malformed_rows, malformed_diagnostics_json, status FROM backfill_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        names = ("job_id", "instrument_id", "epic", "market_name", "timeframe", "requested_start", "requested_end", "current_progress", "last_successful_range", "rows_retrieved", "rows_inserted", "rows_skipped", "malformed_rows", "skipped_malformed_rows", "malformed_diagnostics_json", "status")
        return dict(zip(names, row, strict=True))

    def finish_backfill_job(self, job_id: str, status: str, retrieved: int, inserted: int, skipped: int, malformed_rows: int = 0, skipped_malformed_rows: int = 0, malformed_diagnostics: list[dict] | None = None) -> None:
        self.connection.execute("UPDATE backfill_jobs SET status=?, rows_retrieved=?, rows_inserted=?, rows_skipped=?, malformed_rows=?, skipped_malformed_rows=?, malformed_diagnostics_json=?, updated_at=? WHERE job_id=?", (status, retrieved, inserted, skipped, malformed_rows, skipped_malformed_rows, json.dumps(malformed_diagnostics or [], sort_keys=True, separators=(",", ":")), datetime.now(UTC).isoformat(), job_id))
        self.connection.commit()

    def start_replay_job(self, job_id: str, instrument_id: str, requested_start: str, requested_end: str, window: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute("INSERT INTO replay_jobs (job_id, instrument_id, requested_start, requested_end, window, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?) ON CONFLICT(job_id) DO UPDATE SET status='RUNNING', updated_at=excluded.updated_at", (job_id, instrument_id, requested_start, requested_end, window, now, now))
        self.connection.commit()

    def checkpoint_replay_job(self, job_id: str, information_time: str, snapshots: int, outcomes: int) -> None:
        self.connection.execute("UPDATE replay_jobs SET last_information_time=?, snapshots_generated=?, outcomes_written=?, updated_at=? WHERE job_id=?", (information_time, snapshots, outcomes, datetime.now(UTC).isoformat(), job_id))
        self.connection.commit()

    def get_replay_job(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT job_id, instrument_id, requested_start, requested_end, window, last_information_time, snapshots_generated, outcomes_written, status FROM replay_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        names = ("job_id", "instrument_id", "requested_start", "requested_end", "window", "last_information_time", "snapshots_generated", "outcomes_written", "status")
        return dict(zip(names, row, strict=True))

    def finish_replay_job(self, job_id: str, status: str, processed: int, snapshots: int, outcomes: int) -> None:
        self.connection.execute("UPDATE replay_jobs SET status=?, snapshots_generated=?, outcomes_written=?, updated_at=? WHERE job_id=?", (status, snapshots, outcomes, datetime.now(UTC).isoformat(), job_id))
        self.connection.commit()

    def history_status(self, instrument_id: str | None = None) -> list[dict]:
        query = "SELECT c.instrument_id, c.timeframe, p.source, MIN(c.start_at), MAX(c.start_at), COUNT(*), i.market_name, i.epic FROM candles c JOIN candle_quality q ON q.instrument_id=c.instrument_id AND q.timeframe=c.timeframe AND q.start_at=c.start_at AND q.eligibility='ELIGIBLE' LEFT JOIN candle_provenance p ON p.instrument_id=c.instrument_id AND p.timeframe=c.timeframe AND p.start_at=c.start_at LEFT JOIN instruments i ON i.instrument_id=c.instrument_id WHERE c.is_closed=1"
        params: list[object] = []
        if instrument_id:
            query += " AND c.instrument_id=?"
            params.append(instrument_id)
        query += " GROUP BY c.instrument_id, c.timeframe, p.source ORDER BY c.instrument_id, c.timeframe"
        output = []
        for row in self.connection.execute(query, params):
            jobs = self.connection.execute("SELECT status, requested_start, requested_end, last_successful_range, updated_at FROM backfill_jobs WHERE instrument_id=? AND timeframe=? ORDER BY updated_at DESC LIMIT 1", (row[0], row[1])).fetchone()
            candles = self.list_candles(row[0], row[1])
            gaps = [current.start - previous.end for previous, current in pairwise(candles) if current.start > previous.end]
            output.append({"instrument_id": row[0], "timeframe": row[1], "source": row[2] or "LIVE_AGGREGATED", "earliest": row[3], "latest": row[4], "closed_count": row[5], "market_name": row[6], "epic": row[7], "largest_gap": max(gaps, default=None).total_seconds() if gaps else None, "session_gap_uncertainty": bool(gaps), "backfill_status": jobs[0] if jobs else "NOT_RUN", "requested_start": jobs[1] if jobs else None, "requested_end": jobs[2] if jobs else None, "retrieved_through": jobs[3] if jobs else None, "last_successful_retrieval": jobs[4] if jobs and jobs[0] in {"COMPLETE", "PAUSED"} else None})
        return output

    def list_candles(self, instrument_id: str, timeframe: str, *, through: str | None = None, limit: int | None = None) -> list[Candle]:
        query = (
            "SELECT c.instrument_id, c.timeframe, c.start_at, c.end_at, c.epic, c.open, c.high, c.low, c.close, c.volume, c.is_closed, c.observation_count "
            "FROM candles c JOIN candle_quality q ON q.instrument_id=c.instrument_id AND q.timeframe=c.timeframe AND q.start_at=c.start_at AND q.eligibility='ELIGIBLE' WHERE c.instrument_id = ? AND c.timeframe = ?"
        )
        params: list[str] = [instrument_id, timeframe]
        if through is not None:
            query += " AND c.start_at <= ?"
            params.append(through)
        if limit is not None:
            if limit <= 0:
                return []
            query += " ORDER BY c.start_at DESC LIMIT ?"
            params.append(str(limit))
            rows = list(reversed(self.connection.execute(query, params).fetchall()))
        else:
            query += " ORDER BY c.start_at"
            rows = self.connection.execute(query, params).fetchall()
        return [
            Candle(row[0], row[4], row[1], datetime.fromisoformat(row[2]), datetime.fromisoformat(row[3]), Decimal(row[5]), Decimal(row[6]), Decimal(row[7]), Decimal(row[8]), Decimal(row[9]) if row[9] is not None else None, bool(row[10]), int(row[11]))
            for row in rows
        ]

    @staticmethod
    def _candle_from_row(row: tuple) -> Candle:
        return Candle(
            row[0], row[4], row[1], datetime.fromisoformat(row[2]), datetime.fromisoformat(row[3]),
            Decimal(row[5]), Decimal(row[6]), Decimal(row[7]), Decimal(row[8]),
            Decimal(row[9]) if row[9] is not None else None, bool(row[10]), int(row[11]),
        )

    def iter_candles(self, instrument_id: str, timeframe: str, *, start_at: str | None = None, end_at: str | None = None, batch_size: int = 500):
        """Yield indexed candle batches without materializing a timeframe."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        query = "SELECT c.instrument_id, c.timeframe, c.start_at, c.end_at, c.epic, c.open, c.high, c.low, c.close, c.volume, c.is_closed, c.observation_count FROM candles c JOIN candle_quality q ON q.instrument_id=c.instrument_id AND q.timeframe=c.timeframe AND q.start_at=c.start_at AND q.eligibility='ELIGIBLE' WHERE c.instrument_id=? AND c.timeframe=? AND c.is_closed=1"
        params: list[object] = [instrument_id, timeframe]
        if start_at is not None:
            query += " AND c.start_at>=?"
            params.append(start_at)
        if end_at is not None:
            query += " AND c.start_at<?"
            params.append(end_at)
        query += " ORDER BY c.start_at"
        cursor = self.connection.execute(query, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield self._candle_from_row(row)

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

    def get_direction_snapshots(self, instrument_id: str, start_at: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT snapshot_json FROM direction_snapshots WHERE instrument_id=? AND timeframe='1H' AND candle_start>=? ORDER BY candle_start",
            (instrument_id, start_at),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_direction_snapshot_at(self, instrument_id: str, at_time: str) -> dict | None:
        row = self.connection.execute("SELECT snapshot_json FROM direction_snapshots WHERE instrument_id=? AND timeframe='1H' AND candle_start<=? ORDER BY candle_start DESC LIMIT 1", (instrument_id, at_time)).fetchone()
        return json.loads(row[0]) if row else None

    def get_pattern_observations(self, instrument_id: str, timeframe: str, start_at: str) -> list[dict]:
        rows = self.connection.execute("SELECT candle_start, pattern_name, pattern_instance_id, lifecycle, is_closed, observation_json FROM pattern_observations WHERE instrument_id=? AND timeframe=? AND candle_start>=? ORDER BY candle_start, pattern_instance_id", (instrument_id, timeframe, start_at)).fetchall()
        return [{"candle_start": row[0], "pattern": row[1], "instance_id": row[2], "lifecycle": row[3], "is_closed": bool(row[4]), "observation": json.loads(row[5])} for row in rows]

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

    def list_alerts_for_ui(
        self,
        *,
        instrument_id: str | None = None,
        alert_type: str | None = None,
        read_state: str = "ALL",
        limit: int = 100,
    ) -> list[dict]:
        """Return immutable alert payloads with separately persisted UI state."""
        if read_state not in {"ALL", "UNREAD", "READ"}:
            raise ValueError("read_state must be ALL, UNREAD, or READ")
        query = (
            "SELECT a.payload_json, u.read_at FROM alerts a "
            "LEFT JOIN alert_ui_state u ON u.alert_id=a.alert_id WHERE 1=1"
        )
        params: list[object] = []
        if instrument_id:
            query += " AND a.instrument_id=?"
            params.append(instrument_id)
        if alert_type:
            query += " AND a.alert_type=?"
            params.append(alert_type)
        if read_state == "UNREAD":
            query += " AND u.read_at IS NULL"
        elif read_state == "READ":
            query += " AND u.read_at IS NOT NULL"
        query += " ORDER BY a.created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [
            {**json.loads(payload), "read_at": read_at, "is_read": read_at is not None}
            for payload, read_at in self.connection.execute(query, params).fetchall()
        ]

    def set_alert_read(self, alert_id: str, *, read: bool = True) -> bool:
        """Persist UI-only state; analytical alert rows remain immutable."""
        exists = self.connection.execute("SELECT 1 FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if not exists:
            return False
        read_at = datetime.now(UTC).isoformat() if read else None
        self.connection.execute(
            "INSERT INTO alert_ui_state(alert_id, read_at) VALUES (?, ?) "
            "ON CONFLICT(alert_id) DO UPDATE SET read_at=excluded.read_at",
            (alert_id, read_at),
        )
        self.connection.commit()
        return True

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

    def save_forward_snapshot(self, snapshot: dict) -> bool:
        """Insert once.  LIVE_FORWARD prediction fields are immutable."""
        created_at = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO forward_snapshots
            (snapshot_id, timestamp, reference_time, market, instrument_id, epic,
             source_identity, provenance, timeframe, technical_state_json,
             pattern_state_json, direction_state_json, reference_price, direction,
             up_score, down_score, trend_stage, reversal_state_json, holding_window,
             model_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'LIVE_FORWARD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot["snapshot_id"], snapshot["timestamp"], snapshot["reference_time"],
             snapshot["market"], snapshot["instrument_id"], snapshot["epic"],
             snapshot["source_identity"], snapshot["timeframe"],
             json.dumps(snapshot.get("technical_state", {}), sort_keys=True, separators=(",", ":")),
             json.dumps(snapshot.get("pattern_state", {}), sort_keys=True, separators=(",", ":")),
             json.dumps(snapshot.get("direction_state", {}), sort_keys=True, separators=(",", ":")),
             snapshot["reference_price"], snapshot.get("direction"), snapshot.get("up_score"),
             snapshot.get("down_score"), snapshot.get("trend_stage"),
             json.dumps(snapshot.get("reversal_state"), sort_keys=True, separators=(",", ":")),
             snapshot.get("holding_window"), snapshot["model_version"], created_at),
        )
        if cursor.rowcount == 1:
            self.connection.execute(
                "INSERT OR IGNORE INTO forward_snapshot_status (snapshot_id, eligibility, reason, audited_at) VALUES (?, ?, ?, ?)",
                (snapshot["snapshot_id"], snapshot.get("eligibility", "ELIGIBLE"), snapshot.get("eligibility_reason"), created_at),
            )
        self.connection.commit()
        return cursor.rowcount == 1

    def pending_forward_snapshots(self, instrument_id: str | None = None) -> list[dict]:
        query = """SELECT s.snapshot_id, s.reference_time, s.reference_price, s.direction
                   FROM forward_snapshots s
                   JOIN forward_snapshot_status v ON v.snapshot_id=s.snapshot_id AND v.eligibility='ELIGIBLE'
                   JOIN forward_outcomes o ON o.snapshot_id=s.snapshot_id
                   WHERE o.status='PENDING'"""
        params: list[object] = []
        if instrument_id is not None:
            query += " AND s.instrument_id=?"
            params.append(instrument_id)
        query += " GROUP BY s.snapshot_id ORDER BY s.reference_time"
        return [dict(zip(("snapshot_id", "reference_time", "reference_price", "direction"), row, strict=True)) for row in self.connection.execute(query, params)]

    def save_forward_outcomes(self, snapshot_id: str, outcomes: dict[str, dict]) -> int:
        """Persist mutable outcomes and return newly committed completions only."""
        existing = {
            row[0]: row[1]
            for row in self.connection.execute(
                "SELECT horizon, status FROM forward_outcomes WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchall()
        }
        completed_transitions = 0
        with self.connection:
            for horizon, outcome in outcomes.items():
                cursor = self.connection.execute(
                    """INSERT INTO forward_outcomes
                    (snapshot_id, horizon, status, future_return, future_timestamp, future_price,
                     directional_outcome, mfe, mae, continuation_timing, reversal_timing, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id, horizon) DO UPDATE SET
                      status=excluded.status, future_return=excluded.future_return,
                      future_timestamp=excluded.future_timestamp, future_price=excluded.future_price,
                      directional_outcome=excluded.directional_outcome, mfe=excluded.mfe,
                      mae=excluded.mae, continuation_timing=excluded.continuation_timing,
                      reversal_timing=excluded.reversal_timing, updated_at=excluded.updated_at
                    WHERE forward_outcomes.status != 'COMPLETE'""",
                    (snapshot_id, horizon, outcome.get("status", "PENDING"), outcome.get("percentage_move"),
                     outcome.get("future_timestamp"), outcome.get("future_price"),
                     outcome.get("direction_outcome"), outcome.get("mfe"), outcome.get("mae"),
                     outcome.get("continuation_duration"), outcome.get("time_to_reversal"),
                     datetime.now(UTC).isoformat()),
                )
                if (existing.get(horizon) == "PENDING"
                        and outcome.get("status", "PENDING") == "COMPLETE"
                        and cursor.rowcount == 1):
                    completed_transitions += 1
        return completed_transitions

    def get_forward_state(self, instrument_id: str | None = None) -> dict | None:
        query = "SELECT s.* FROM forward_snapshots s JOIN forward_snapshot_status v ON v.snapshot_id=s.snapshot_id AND v.eligibility='ELIGIBLE'"
        params: list[object] = []
        if instrument_id:
            query += " WHERE instrument_id=?"
            params.append(instrument_id)
        query += " ORDER BY reference_time DESC LIMIT 1"
        row = self.connection.execute(query, params).fetchone()
        if row is None:
            return None
        columns = [item[1] for item in self.connection.execute("PRAGMA table_info(forward_snapshots)")]
        result = dict(zip(columns, row, strict=True))
        for key in ("technical_state_json", "pattern_state_json", "direction_state_json", "reversal_state_json"):
            result[key.removesuffix("_json")] = json.loads(result[key])
        return result

    def save_decision_telemetry(self, decision: dict) -> str:
        """Persist an append-safe decision snapshot without changing market history."""
        import hashlib

        telemetry = decision.get("telemetry", {})
        identity = json.dumps(telemetry, sort_keys=True, separators=(",", ":"))
        telemetry_id = hashlib.sha256(identity.encode()).hexdigest()
        self.connection.execute(
            """INSERT OR IGNORE INTO decision_telemetry
            (telemetry_id, reference_time, instrument_id, side, regime,
             primary_action_state, model_version, source_identity, telemetry_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                telemetry_id,
                telemetry.get("timestamp") or decision.get("reference_time"),
                telemetry.get("instrument") or decision.get("instrument"),
                telemetry.get("side"),
                telemetry.get("1H_regime"),
                telemetry.get("primary_action_state") or decision["primary_action_state"],
                telemetry.get("model_version") or decision.get("schema_version"),
                telemetry.get("provenance") or decision.get("source"),
                json.dumps(telemetry, sort_keys=True, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()
        return telemetry_id

    def get_decision_telemetry(self, instrument_id: str, *, limit: int = 20) -> list[dict]:
        rows = self.connection.execute(
            "SELECT telemetry_json FROM decision_telemetry WHERE instrument_id=? "
            "ORDER BY reference_time DESC LIMIT ?",
            (instrument_id, max(1, limit)),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def research_source_identity(self, instrument_id: str) -> str:
        row = self.connection.execute("SELECT epic, market_name, instrument_type, metadata_json FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
        if row:
            return f"ig_cfd|instrument={instrument_id}|epic={row[0]}|market={row[1]}|type={row[2] or ''}"
        return f"ig_cfd|instrument={instrument_id}|epic=unknown"

    def save_research_snapshot(self, snapshot: dict) -> None:
        columns = ("snapshot_id", "instrument_id", "timeframe", "snapshot_timestamp", "reference_time", "snapshot_type", "source_identity", "reference_price", "direction", "up_score", "down_score", "coverage_json", "trend_stage", "reversal_risk", "holding_window", "agreement", "four_hour_alignment", "pattern_name", "pattern_lifecycle", "instance_identity", "divergence_indicator", "divergence_direction", "alert_type", "alert_id", "alert_event_identity", "score_bucket", "trend_regime", "volatility_regime", "context_json", "feature_version", "pattern_version", "direction_version", "alert_version", "research_version", "created_at")
        values = (snapshot["snapshot_id"], snapshot["instrument_id"], snapshot["timeframe"], snapshot["snapshot_timestamp"], snapshot["reference_time"], snapshot["snapshot_type"], snapshot["source_identity"], snapshot["reference_price"], snapshot.get("direction"), snapshot.get("up_score"), snapshot.get("down_score"), json.dumps(snapshot.get("coverage"), sort_keys=True, separators=(",", ":")), snapshot.get("trend_stage"), snapshot.get("reversal_risk"), snapshot.get("holding_window"), snapshot.get("agreement"), snapshot.get("four_hour_alignment"), snapshot.get("pattern_name"), snapshot.get("pattern_lifecycle"), snapshot.get("instance_identity"), snapshot.get("divergence_indicator"), snapshot.get("divergence_direction"), snapshot.get("alert_type"), snapshot.get("alert_id"), snapshot.get("alert_event_identity"), snapshot.get("score_bucket"), snapshot["trend_regime"], snapshot["volatility_regime"], snapshot["context_json"], snapshot.get("feature_version"), snapshot.get("pattern_version"), snapshot.get("direction_version"), snapshot.get("alert_version"), snapshot["research_version"], datetime.now(UTC).isoformat())
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(f"INSERT INTO research_snapshots ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(snapshot_id) DO UPDATE SET reference_price=excluded.reference_price, context_json=excluded.context_json, trend_regime=excluded.trend_regime, volatility_regime=excluded.volatility_regime", values)
        self.connection.execute(
            "INSERT INTO research_regimes VALUES (?, ?, ?, ?) ON CONFLICT(snapshot_id) DO UPDATE SET trend_regime=excluded.trend_regime, volatility_regime=excluded.volatility_regime, methodology_version=excluded.methodology_version",
            (snapshot["snapshot_id"], snapshot["trend_regime"], snapshot["volatility_regime"], snapshot["research_version"]),
        )
        self.connection.commit()

    def _save_research_outcome(self, connection: sqlite3.Connection, snapshot_id: str, horizon: str, outcome: dict) -> None:
        connection.execute(
            """INSERT INTO research_outcomes
            (snapshot_id, horizon, status, future_timestamp, future_price, absolute_move, percentage_move,
             direction_outcome, mfe, mae, time_to_mfe, time_to_mae, time_to_reversal, continuation_duration, direction_side_held, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id, horizon) DO UPDATE SET status=excluded.status, future_timestamp=excluded.future_timestamp,
            future_price=excluded.future_price, absolute_move=excluded.absolute_move, percentage_move=excluded.percentage_move,
            direction_outcome=excluded.direction_outcome, mfe=excluded.mfe, mae=excluded.mae, time_to_mfe=excluded.time_to_mfe,
            time_to_mae=excluded.time_to_mae, time_to_reversal=excluded.time_to_reversal, continuation_duration=excluded.continuation_duration,
            direction_side_held=excluded.direction_side_held, updated_at=excluded.updated_at""",
            (snapshot_id, horizon, outcome.get("status", "PENDING"), outcome.get("future_timestamp"), outcome.get("future_price"), outcome.get("absolute_move"), outcome.get("percentage_move"), outcome.get("direction_outcome"), outcome.get("mfe"), outcome.get("mae"), outcome.get("time_to_mfe"), outcome.get("time_to_mae"), outcome.get("time_to_reversal"), outcome.get("continuation_duration"), None if outcome.get("direction_side_held") is None else int(outcome["direction_side_held"]), datetime.now(UTC).isoformat()),
        )

    def save_research_outcome(self, snapshot_id: str, horizon: str, outcome: dict) -> None:
        with self.connection:
            self._save_research_outcome(self.connection, snapshot_id, horizon, outcome)

    def save_research_outcomes(self, snapshot_id: str, outcomes: dict[str, dict]) -> None:
        with self.connection:
            for horizon, outcome in outcomes.items():
                self._save_research_outcome(self.connection, snapshot_id, horizon, outcome)
        self.connection.commit()

    def get_research_snapshots(self, *, instrument_id: str | None = None, timeframe: str | None = None, snapshot_type: str | None = None, window_start: str | None = None, direction: str | None = None, score_bucket: str | None = None, trend_stage: str | None = None, reversal_risk: str | None = None, agreement: str | None = None, four_hour_alignment: str | None = None, pattern_name: str | None = None, alert_type: str | None = None) -> list[dict]:
        query = "SELECT * FROM research_snapshots WHERE 1=1"
        params: list[object] = []
        for column, value in (("instrument_id", instrument_id), ("timeframe", timeframe), ("snapshot_type", snapshot_type), ("direction", direction), ("score_bucket", score_bucket), ("trend_stage", trend_stage), ("reversal_risk", reversal_risk), ("agreement", agreement), ("four_hour_alignment", four_hour_alignment), ("pattern_name", pattern_name), ("alert_type", alert_type)):
            if value is not None:
                query += f" AND {column}=?"
                params.append(value)
        if window_start is not None:
            query += " AND snapshot_timestamp>=?"
            params.append(window_start)
        query += " ORDER BY snapshot_timestamp"
        rows = self.connection.execute(query, params).fetchall()
        columns = [column[1] for column in self.connection.execute("PRAGMA table_info(research_snapshots)")]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def research_summary(self, *, instrument_id: str | None = None, timeframe: str | None = None, direction: str | None = None, score_bucket: str | None = None, trend_stage: str | None = None, reversal_risk: str | None = None, agreement: str | None = None, four_hour_alignment: str | None = None, pattern_name: str | None = None, alert_type: str | None = None, horizon: str | None = None, window_start: str | None = None) -> list[dict]:
        snapshots = self.get_research_snapshots(instrument_id=instrument_id, timeframe=timeframe, direction=direction, score_bucket=score_bucket, trend_stage=trend_stage, reversal_risk=reversal_risk, agreement=agreement, four_hour_alignment=four_hour_alignment, pattern_name=pattern_name, alert_type=alert_type, window_start=window_start)
        if not snapshots:
            return []
        snapshot_ids = [row["snapshot_id"] for row in snapshots]
        placeholders = ",".join("?" for _ in snapshot_ids)
        query = f"SELECT snapshot_id, horizon, direction_outcome, percentage_move, mfe, mae FROM research_outcomes WHERE snapshot_id IN ({placeholders})"
        params: list[object] = snapshot_ids
        if horizon:
            query += " AND horizon=?"
            params.append(horizon)
        rows = self.connection.execute(query, params).fetchall()
        grouped: dict[str, list[tuple]] = {}
        for row in rows:
            grouped.setdefault(row[1], []).append(row[0:1] + row[2:])
        output = []
        for key, values in sorted(grouped.items()):
            complete = [value for value in values if value[1] is not None]
            count = len(complete)
            up_count = sum(value[1] == "UP" for value in complete)
            down_count = sum(value[1] == "DOWN" for value in complete)
            flat_count = sum(value[1] == "FLAT" for value in complete)
            output.append({"horizon": key, "sample_count": count, "snapshot_count": len({value[0] for value in complete}), "up_count": up_count, "down_count": down_count, "flat_count": flat_count, "up_percentage": up_count / count * 100 if count else None, "down_percentage": down_count / count * 100 if count else None, "flat_percentage": flat_count / count * 100 if count else None, "average_move": sum(value[2] for value in complete) / count if count else None, "median_mfe": median([value[3] for value in complete if value[3] is not None]) if complete and any(value[3] is not None for value in complete) else None, "median_mae": median([value[4] for value in complete if value[4] is not None]) if complete and any(value[4] is not None for value in complete) else None})
        return output

    def completed_research_snapshot_count(self, **filters: object) -> int:
        snapshots = self.get_research_snapshots(**filters)
        if not snapshots:
            return 0
        placeholders = ",".join("?" for _ in snapshots)
        return self.connection.execute(f"SELECT COUNT(DISTINCT snapshot_id) FROM research_outcomes WHERE status='COMPLETE' AND snapshot_id IN ({placeholders})", [row["snapshot_id"] for row in snapshots]).fetchone()[0]

    def research_4h_comparison(self, *, instrument_id: str | None = None, timeframe: str = "1H", horizon: str | None = None) -> dict[str, dict[str, int] | str]:
        params: list[object] = [timeframe]
        query = "SELECT snapshot_id, four_hour_alignment FROM research_snapshots WHERE timeframe=?"
        if instrument_id:
            query += " AND instrument_id=?"
            params.append(instrument_id)
        groups = {"WITH_4H": [], "WITHOUT_4H": []}
        for snapshot_id, alignment in self.connection.execute(query, params):
            groups["WITH_4H" if alignment is not None else "WITHOUT_4H"].append(snapshot_id)
        output: dict[str, dict[str, int] | str] = {"status": "READINESS_ONLY — no incremental value claim"}
        for name, ids in groups.items():
            ids = list(dict.fromkeys(ids))
            if not ids:
                output[name] = {"snapshot_count": 0, "outcome_count": 0}
                continue
            placeholders = ",".join("?" for _ in ids)
            sql = f"SELECT COUNT(*) FROM research_outcomes WHERE status='COMPLETE' AND snapshot_id IN ({placeholders})"
            values: list[object] = [*ids]
            if horizon:
                sql += " AND horizon=?"
                values.append(horizon)
            output[name] = {"snapshot_count": len(ids), "outcome_count": self.connection.execute(sql, values).fetchone()[0]}
        return output

    def available_history(self, instrument_id: str, timeframe: str, requested_window: str | None = None) -> dict:
        row = self.connection.execute("SELECT MIN(start_at), MAX(start_at), COUNT(*) FROM candles WHERE instrument_id=? AND timeframe=? AND is_closed=1", (instrument_id, timeframe)).fetchone()
        if not row or row[0] is None:
            return {"first": None, "last": None, "candle_count": 0, "days": 0.0, "status": "INSUFFICIENT_HISTORY"}
        first, last = datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])
        days = max(0.0, (last - first).total_seconds() / 86400)
        required_days = int(requested_window[:-1]) * 365 if requested_window and requested_window.endswith("Y") and requested_window[:-1].isdigit() else None
        status = "INSUFFICIENT_HISTORY" if required_days is not None and days < required_days else "AVAILABLE"
        return {"first": row[0], "last": row[1], "candle_count": row[2], "days": days, "requested_days": required_days, "status": status}

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
