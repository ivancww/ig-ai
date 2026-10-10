"""Small read-only HTTP/PWA layer for persisted IG AI state."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .build_identity import load_build_identity
from .database import Database
from .health_contract import HealthContract
from .market_identity import CANONICAL_MARKETS, canonical_instrument_rows
from .self_monitoring import evaluate_health


def _resolve_web_dir(module_path: Path = Path(__file__), prefix: str = sys.prefix) -> Path:
    """Use installed wheel data, while preserving source-checkout behavior."""
    installed = Path(prefix) / "share" / "ig-ai" / "web"
    if installed.is_dir():
        return installed
    return module_path.resolve().parents[2] / "web"


WEB_DIR = _resolve_web_dir()
MARKETS = CANONICAL_MARKETS
ALERT_TYPES = {
    "DIRECTION_SHIFT", "REVERSAL_CONFIRMED", "REVERSAL_WATCH", "REVERSAL_RISK_INCREASE",
    "REVERSAL_RISK_DECREASE", "EARLY_REVERSAL_WARNING", "TIMEFRAME_REALIGNMENT",
    "TIMEFRAME_CONFLICT", "FALSE_BREAKOUT", "BREAKOUT_CONFIRMED", "RESISTANCE_BREAK",
    "SUPPORT_BREAK", "PATTERN_NEAR_CONFIRMATION", "PATTERN_CONFIRMED", "PATTERN_FAILED",
    "VOLATILITY_EXPANSION", "HOLDING_WINDOW_SHORTENED", "HOLDING_WINDOW_EXTENDED",
    "TREND_STAGE_CHANGE",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")


def _value(value: object, fallback: str = "UNKNOWN") -> object:
    return fallback if value in (None, "", []) else value


def _freshness(state: str | None, timestamp: str | None, stale_seconds: float = 900.0) -> str:
    if state in {"MARKET_CLOSED", "SCHEDULED_OFF", "STALE_DATA"}:
        return {"MARKET_CLOSED": "MARKET CLOSED", "SCHEDULED_OFF": "SCHEDULED OFF", "STALE_DATA": "STALE"}[state]
    if not timestamp:
        return "UNKNOWN"
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(timestamp).astimezone(UTC)).total_seconds()
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "LIVE / CURRENT" if age <= stale_seconds else "STALE"


def _analysis_freshness(timestamp: str | None, stale_seconds: float = 900.0) -> str:
    return _freshness(None, timestamp, stale_seconds) if timestamp else "WAITING"


class WebReadModel:
    """Translate persisted engine records into a stable, browser-safe contract."""

    def __init__(
        self,
        database: Database,
        *,
        heartbeat_seconds: float | None = None,
        stale_seconds: float | None = None,
    ):
        self.database = database
        persisted_contract = (database.get_runtime_state("self_monitoring") or {}).get(
            "health_contract", {}
        )
        heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else float(
                os.environ.get(
                    "IGAI_SERVICE_HEARTBEAT_SECONDS",
                    persisted_contract.get("heartbeat_seconds", "30"),
                )
            )
        )
        stale_seconds = (
            stale_seconds
            if stale_seconds is not None
            else float(
                os.environ.get(
                    "IGAI_STALE_DATA_SECONDS",
                    persisted_contract.get("stale_data_seconds", "900"),
                )
            )
        )
        self.health_contract = HealthContract(
            heartbeat_seconds=heartbeat_seconds,
            stale_data_seconds=stale_seconds,
        )
        self.heartbeat_seconds = self.health_contract.heartbeat_seconds
        self.stale_seconds = self.health_contract.stale_data_seconds

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value).astimezone(UTC)
        except ValueError:
            return None

    def _instrument_rows(self) -> dict[str, tuple[str, str, str | None]]:
        rows = self.database.connection.execute(
            "SELECT instrument_id, market_name, market_status, epic, metadata_json FROM instruments"
        ).fetchall()
        return canonical_instrument_rows(rows)

    def _instrument_id(self, market: str, instruments: dict[str, tuple[str, str, str | None]]) -> str | None:
        """Resolve a user-facing market label to the persisted instrument id."""
        identity = instruments.get(market)
        return identity[0] if identity else None

    def _latest_telemetry(self, instrument_id: str | None, market: str) -> dict:
        for identity in (instrument_id, market):
            if identity:
                rows = self.database.get_decision_telemetry(identity, limit=1)
                if rows:
                    return rows[0]
        return {}

    def _market(self, market: str, instruments: dict[str, tuple[str, str, str | None]]) -> dict:
        instrument_id, _epic, market_status = instruments.get(market, (None, None, None))
        runtime = next((x for x in self.database.list_runtime_market_states() if x.get("instrument_id") == instrument_id), {}) if instrument_id else {}
        market_status = runtime.get("market_status", market_status)
        # Phase 8B snapshots identify the analytical market; runtime rows use
        # the provider instrument id. Support both persisted identities.
        direction = (self.database.get_direction_status(instrument_id) if instrument_id else None) or self.database.get_direction_status(market)
        monitor = (self.database.get_monitor_state(instrument_id) if instrument_id else None) or self.database.get_monitor_state(market)
        telemetry = self._latest_telemetry(instrument_id, market)
        observation = None
        if instrument_id:
            row = self.database.connection.execute(
                "SELECT observed_at, mid, bid, offer, market_state FROM observations "
                "WHERE instrument_id=? ORDER BY observed_at DESC LIMIT 1", (instrument_id,)
            ).fetchone()
            if row:
                observation = {"observed_at": row[0], "price": row[1], "bid": row[2], "offer": row[3], "market_state": row[4]}
        trend = {"UP": "BULLISH", "DOWN": "BEARISH"}.get((direction or {}).get("direction"), "INSUFFICIENT EVIDENCE" if direction else "UNKNOWN")
        warning_items = (monitor or {}).get("early_warning", [])
        warning = "WARNING PRESENT" if warning_items else "NO CURRENT WARNING" if monitor else "UNKNOWN"
        reference_time = (direction or {}).get("candle_timestamp") or observation and observation["observed_at"]
        state = runtime.get("monitoring_state")
        live_timestamp = (observation or {}).get("observed_at") or runtime.get("last_tick")
        selected_side = telemetry.get("evaluated_side") or telemetry.get("side") or None
        entry_score = telemetry.get("entry_quality_score")
        market_telemetry = (
            self.database.latest_market_telemetry(instrument_id)
            if instrument_id
            else {
                "last_closed_15m_at": None,
                "last_closed_1h_at": None,
                "last_decision_at": None,
                "last_alert_at": None,
            }
        )
        return {
            "market": market,
            "instrument_id": instrument_id,
            "price": _value((observation or {}).get("price")),
            "price_timestamp": (observation or {}).get("observed_at"),
            "trend_1h": trend,
            "warning_15m": warning,
            "warning_details": warning_items,
            "regime": _value(telemetry.get("1H_regime")),
            "market_bias": _value(telemetry.get("market_bias")),
            "entry_quality": entry_score if entry_score is not None else "NOT AVAILABLE",
            "entry_quality_state": _value(telemetry.get("entry_quality_qualifier")),
            "entry_quality_is_probability": False,
            "primary_action": _value(telemetry.get("primary_action_state"), "NOT AVAILABLE"),
            "structure_invalidation": _value(telemetry.get("structure_invalidation_state"), "NOT AVAILABLE"),
            "profit_protection": _value(telemetry.get("profit_protection_state"), "NOT AVAILABLE"),
            "runner_state": _value(telemetry.get("big_wave_qualification_state"), "NOT AVAILABLE"),
            "evaluated_side": _value(telemetry.get("evaluated_side"), "NOT AVAILABLE"),
            "last_updated": reference_time,
            "live_updated_at": live_timestamp,
            "analysis_updated_at": reference_time,
            "freshness": _freshness(state, live_timestamp, self.stale_seconds),
            "live_freshness": _freshness(state, live_timestamp, self.stale_seconds),
            "analysis_freshness": _analysis_freshness(reference_time, self.stale_seconds),
            "monitoring_state": _value(state),
            "market_status": _value(market_status),
            "data_quality": _value((direction or {}).get("coverage", {}).get("state"), "UNKNOWN"),
            "selected_side": selected_side,
            **market_telemetry,
        }

    def dashboard(self) -> dict:
        instruments = self._instrument_rows()
        markets = []
        for market in MARKETS:
            public = self._market(market, instruments)
            public.pop("instrument_id", None)
            markets.append(public)
        return {"markets": markets, "read_only": True, "generated_at": datetime.now(UTC).isoformat()}

    def alerts(self, *, market: str | None = None, category: str | None = None, state: str = "ALL") -> dict:
        if market and market not in MARKETS:
            raise ValueError("invalid market")
        if category and category not in ALERT_TYPES:
            raise ValueError("invalid alert category")
        instruments = self._instrument_rows()
        persisted_identity = self._instrument_id(market, instruments) if market else None
        if market and persisted_identity is None:
            raise ValueError("market identity unavailable")
        rows = self.database.list_alerts_for_ui(instrument_id=persisted_identity, alert_type=category, read_state=state, limit=100)
        names = {identity[0]: name for name, identity in instruments.items()}
        public_rows = []
        for row in rows:
            public = {
                key: row.get(key)
                for key in (
                    "alert_id", "alert_type", "priority", "created_at", "model_reference_time",
                    "trigger_time", "current_direction", "current_trend_stage", "current_reversal_risk",
                    "current_holding_window", "trigger_evidence", "confirmed", "state", "message",
                    "read_at", "is_read",
                )
            }
            public["market"] = names.get(row.get("instrument"), "UNKNOWN")
            public_rows.append(public)
        return {"alerts": public_rows, "state": state, "generated_at": datetime.now(UTC).isoformat()}

    def health(self) -> dict:
        runtime = self.database.get_runtime_state("service") or {}
        markets = self.database.list_runtime_market_states()
        persisted_snapshot = self.database.get_runtime_state("self_monitoring")
        current_generation = runtime.get("runtime_generation")
        started_at = runtime.get("service_started_at")
        snapshot_is_current = bool(persisted_snapshot)
        if current_generation:
            now = datetime.now(UTC)
            snapshot_time = self._timestamp(persisted_snapshot.get("generated_at")) if persisted_snapshot else None
            heartbeat_time = self._timestamp(persisted_snapshot.get("service", {}).get("heartbeat")) if persisted_snapshot else None
            started_time = self._timestamp(started_at)
            snapshot_is_current = bool(
                persisted_snapshot
                and persisted_snapshot.get("runtime_generation") == current_generation
                and snapshot_time
                and persisted_snapshot.get("generation_status") == "CURRENT"
                and started_time
                and snapshot_time >= started_time
                and (now - snapshot_time).total_seconds() <= self.health_contract.heartbeat_max_age_seconds
                and heartbeat_time
                and (now - heartbeat_time).total_seconds() <= self.health_contract.heartbeat_max_age_seconds
            )
        snapshot = persisted_snapshot if snapshot_is_current else evaluate_health(
            self.database,
            stale_seconds=self.stale_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            expected_generation=current_generation,
        )
        if current_generation and not snapshot_is_current:
            snapshot = dict(snapshot)
            snapshot["self_monitoring_ready"] = False
            incidents = list(snapshot.get("incidents", []))
            if not any(item.get("id") == "self-monitor-generation" for item in incidents):
                incidents.append({
                    "id": "self-monitor-generation",
                    "severity": "DEGRADED",
                    "domain": "SERVICE",
                    "reason": "current runtime self-monitor snapshot is not persisted yet",
                    "status": "ACTIVE",
                })
            snapshot["incidents"] = incidents
            snapshot["overall_status"] = "DEGRADED"
        else:
            heartbeat = snapshot.get("service", {}).get("heartbeat")
            heartbeat_time = self._timestamp(heartbeat)
            started_time = self._timestamp(started_at)
            heartbeat_current = bool(
                heartbeat_time
                and (datetime.now(UTC) - heartbeat_time).total_seconds() <= self.health_contract.heartbeat_max_age_seconds
            )
            snapshot = {
                **snapshot,
                "self_monitoring_ready": bool(
                    heartbeat_current
                    and runtime.get("status") == "HEALTHY"
                    and runtime.get("ig_connection") == "CONNECTED"
                    and (not current_generation or (heartbeat_time and started_time and heartbeat_time >= started_time))
                ),
            }
        if runtime.get("status") != "HEALTHY" or runtime.get("ig_connection") != "CONNECTED":
            snapshot = dict(snapshot)
            incidents = list(snapshot.get("incidents", []))
            if not any(item.get("id") == "runtime-readiness" for item in incidents):
                incidents.append({
                    "id": "runtime-readiness",
                    "severity": "DEGRADED",
                    "domain": "SERVICE",
                    "reason": "current backend runtime is not healthy and IG-connected",
                    "status": "ACTIVE",
                })
            snapshot["incidents"] = incidents
            snapshot["overall_status"] = "DEGRADED"
        schedules = {item["instrument_id"]: item for item in self.database.list_runtime_schedules()}
        names = {identity[0]: market for market, identity in self._instrument_rows().items()}
        for market in markets:
            market["market"] = names.get(market.get("instrument_id"), "UNKNOWN")
            market["schedule"] = schedules.get(market.get("instrument_id"), {"mode": "UNKNOWN", "windows": [], "timezone": "UNKNOWN"})
            market["stale_warning"] = market.get("monitoring_state") == "STALE_DATA"
        public_schedules = [{key: value for key, value in {**schedule, "market": names.get(schedule.get("instrument_id"), "UNKNOWN")}.items() if key != "instrument_id"} for schedule in schedules.values()]
        public_markets = [{key: value for key, value in market.items() if key != "instrument_id"} for market in markets]
        health_markets = snapshot.get("markets") or public_markets
        service_payload = {
            **snapshot.get("service", {}),
            "status": _value(runtime.get("status")),
            "ig_connection": _value(runtime.get("ig_connection")),
            "last_heartbeat": runtime.get("last_heartbeat_at"),
        }
        ig_payload = {
            **snapshot.get("ig", {}),
            "connection_status": _value(runtime.get("ig_connection")),
        }
        return {
            "generated_at": snapshot.get("generated_at", datetime.now(UTC).isoformat()),
            "overall_status": snapshot.get("overall_status", "UNKNOWN"),
            "service": service_payload,
            "self_monitoring": {
                "ready": snapshot.get("self_monitoring_ready", False),
                "runtime_generation": snapshot.get("runtime_generation"),
                "generated_at": snapshot.get("generated_at"),
            },
            "ig": ig_payload,
            "database": snapshot.get("database", {"status": "UNKNOWN"}),
            "health_contract": self.health_contract.as_dict(),
            "build": load_build_identity(),
            "alerts": snapshot.get("alerts", {"status": "UNKNOWN"}),
            "incidents": snapshot.get("incidents", []),
            "markets": health_markets,
            "schedules": public_schedules,
        }


class WebHandler(BaseHTTPRequestHandler):
    server_version = "IGAIWeb/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    @property
    def model(self) -> WebReadModel:
        return self.server.model  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/dashboard":
                return self._json(self.model.dashboard())
            if parsed.path == "/api/alerts":
                query = parse_qs(parsed.query)
                state = query.get("state", ["ALL"])[0].upper()
                return self._json(self.model.alerts(market=query.get("market", [None])[0], category=query.get("category", [None])[0], state=state))
            if parsed.path == "/api/health":
                return self._json(self.model.health())
            if parsed.path == "/api/schedule":
                return self._json({"schedules": self.model.health()["schedules"], "write_supported": False, "note": "The service remains online; schedules control when analysis is active."})
            if parsed.path == "/manifest.webmanifest":
                return self._static("manifest.webmanifest")
            if parsed.path in {"/", "/index.html"} or parsed.path.startswith(("/assets/", "/icons/")):
                return self._static(parsed.path.lstrip("/") or "index.html")
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            return self._json({"error": "data unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/alerts/([A-Za-z0-9_.:-]{1,180})/(read|unread)", parsed.path)
        if not match or not SAFE_ID.fullmatch(match.group(1)):
            return self._json({"error": "invalid endpoint"}, HTTPStatus.BAD_REQUEST)
        if not self.model.database.set_alert_read(match.group(1), read=match.group(2) == "read"):
            return self._json({"error": "alert not found"}, HTTPStatus.NOT_FOUND)
        return self._json({"ok": True, "alert_id": match.group(1), "is_read": match.group(2) == "read"})

    def _static(self, relative: str) -> None:
        path = (WEB_DIR / relative).resolve()
        if WEB_DIR not in path.parents or not path.is_file():
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(database_path: str | Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    database = Database(database_path)
    # Keep the SQLite connection on its owner thread. A reverse proxy can
    # provide concurrency while this small first-party viewer stays safe.
    server = HTTPServer((host, port), WebHandler)
    heartbeat_seconds = float(os.environ.get("IGAI_SERVICE_HEARTBEAT_SECONDS", "30"))
    stale_seconds = float(os.environ.get("IGAI_STALE_DATA_SECONDS", "900"))
    server.model = WebReadModel(  # type: ignore[attr-defined]
        database,
        heartbeat_seconds=heartbeat_seconds,
        stale_seconds=stale_seconds,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ig-ai-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    database_path = args.database or os.environ.get("IG_DATABASE_PATH", "data/ig_ai.sqlite3")
    host = args.host if args.host != "127.0.0.1" else os.environ.get("IGAI_WEB_HOST", args.host)
    port = args.port if args.port != 8080 else int(os.environ.get("IGAI_WEB_PORT", str(args.port)))
    serve(database_path, host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
