"""Small read-only HTTP/PWA layer for persisted IG AI state."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .database import Database

PROJECT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_DIR / "web"
MARKETS = ("US Tech 100", "Japan 225", "Hong Kong HS50")
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


def _freshness(state: str | None, timestamp: str | None) -> str:
    if state in {"MARKET_CLOSED", "SCHEDULED_OFF", "STALE_DATA"}:
        return {"MARKET_CLOSED": "MARKET CLOSED", "SCHEDULED_OFF": "SCHEDULED OFF", "STALE_DATA": "STALE"}[state]
    if not timestamp:
        return "UNKNOWN"
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(timestamp).astimezone(UTC)).total_seconds()
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "LIVE / CURRENT" if age <= 900 else "STALE"


class WebReadModel:
    """Translate persisted engine records into a stable, browser-safe contract."""

    def __init__(self, database: Database):
        self.database = database

    def _instrument_rows(self) -> dict[str, tuple[str, str, str | None]]:
        rows = self.database.connection.execute(
            "SELECT instrument_id, market_name, market_status, epic FROM instruments"
        ).fetchall()
        return {row[1]: (row[0], row[3], row[2]) for row in rows}

    def _market(self, market: str, instruments: dict[str, tuple[str, str, str | None]]) -> dict:
        instrument_id, _epic, market_status = instruments.get(market, (None, None, None))
        runtime = next((x for x in self.database.list_runtime_market_states() if x.get("instrument_id") == instrument_id), {}) if instrument_id else {}
        # Phase 8B snapshots identify the analytical market; runtime rows use
        # the provider instrument id. Support both persisted identities.
        direction = (self.database.get_direction_status(instrument_id) if instrument_id else None) or self.database.get_direction_status(market)
        monitor = self.database.get_monitor_state(market) if market else None
        telemetry = self.database.get_decision_telemetry(instrument_id, limit=1)[0] if instrument_id and self.database.get_decision_telemetry(instrument_id, limit=1) else {}
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
        selected_side = "LONG" if telemetry.get("side") == "LONG" or telemetry.get("1H_structure") == "UP" else "SHORT" if telemetry.get("side") == "SHORT" or telemetry.get("1H_structure") == "DOWN" else None
        entry_score = telemetry.get("entry_quality_score")
        return {
            "market": market,
            "instrument_id": instrument_id,
            "price": _value((observation or {}).get("price")),
            "price_timestamp": (observation or {}).get("observed_at"),
            "trend_1h": trend,
            "warning_15m": warning,
            "warning_details": warning_items,
            "regime": _value(telemetry.get("1H_regime")),
            "market_bias": {"UP": "BULLISH", "DOWN": "BEARISH"}.get((direction or {}).get("direction"), _value(telemetry.get("side"), "UNKNOWN")),
            "entry_quality": entry_score if entry_score is not None else "NOT AVAILABLE",
            "entry_quality_is_probability": False,
            "primary_action": _value(telemetry.get("primary_action_state"), "NOT AVAILABLE"),
            "structure_invalidation": _value((direction or {}).get("reversal_risk", {}).get("category"), "NOT AVAILABLE"),
            "profit_protection": "NOT AVAILABLE",
            "runner_state": "NOT AVAILABLE",
            "last_updated": reference_time,
            "freshness": _freshness(state, reference_time),
            "monitoring_state": _value(state),
            "market_status": _value(market_status),
            "data_quality": _value((direction or {}).get("coverage", {}).get("state"), "UNKNOWN"),
            "selected_side": selected_side,
        }

    def dashboard(self) -> dict:
        instruments = self._instrument_rows()
        return {"markets": [self._market(market, instruments) for market in MARKETS], "read_only": True, "generated_at": datetime.now(UTC).isoformat()}

    def alerts(self, *, market: str | None = None, category: str | None = None, state: str = "ALL") -> dict:
        if market and market not in MARKETS:
            raise ValueError("invalid market")
        if category and category not in ALERT_TYPES:
            raise ValueError("invalid alert category")
        rows = self.database.list_alerts_for_ui(instrument_id=market, alert_type=category, read_state=state, limit=100)
        return {"alerts": rows, "state": state, "generated_at": datetime.now(UTC).isoformat()}

    def health(self) -> dict:
        runtime = self.database.get_runtime_state("service") or {}
        markets = self.database.list_runtime_market_states()
        schedules = {item["instrument_id"]: item for item in self.database.list_runtime_schedules()}
        for market in markets:
            market["schedule"] = schedules.get(market.get("instrument_id"), {"mode": "UNKNOWN", "windows": [], "timezone": "UNKNOWN"})
            market["stale_warning"] = market.get("monitoring_state") == "STALE_DATA"
        return {"service": {"status": _value(runtime.get("status")), "ig_connection": _value(runtime.get("ig_connection")), "last_heartbeat": runtime.get("last_heartbeat_at")}, "markets": markets, "schedules": list(schedules.values()), "generated_at": datetime.now(UTC).isoformat()}


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
    server = ThreadingHTTPServer((host, port), WebHandler)
    server.model = WebReadModel(database)  # type: ignore[attr-defined]
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
    settings = Settings.from_env(require_credentials=False)
    serve(args.database or settings.database_path, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
