import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ig_ai.database import Database
from ig_ai.direction import DIRECTION_SCHEMA_VERSION
from ig_ai.web import WEB_DIR, WebReadModel


def _seed_market(database: Database, *, stale: bool = False) -> None:
    observed = datetime.now(UTC) - (timedelta(hours=2) if stale else timedelta(seconds=10))
    database.save_instrument("EPIC", "EPIC", "US Tech 100", market_status="OPEN")
    database.save_observation(
        __import__("ig_ai.models", fromlist=["MarketObservation"]).MarketObservation(
            timestamp=observed, instrument_id="EPIC", epic="EPIC", market_name="US Tech 100", bid=100, offer=101
        )
    )
    database.save_runtime_state("service", {"status": "HEALTHY", "ig_connection": "CONNECTED"})
    database.save_runtime_market_state("EPIC", {"instrument_id": "EPIC", "monitoring_state": "STALE_DATA" if stale else "MONITORING", "last_tick": observed.isoformat()})
    database.save_direction_snapshot({
        "instrument": "US Tech 100", "timeframe": "1H", "candle_timestamp": observed.isoformat(),
        "direction": "UP", "coverage": {"ratio": 0.8}, "reversal_risk": {"category": "LOW"},
        "schema_version": DIRECTION_SCHEMA_VERSION, "candle_state": "CLOSED",
    })
    database.save_decision_telemetry({"instrument": "EPIC", "reference_time": observed.isoformat(), "primary_action_state": "NO_TRADE", "telemetry": {"instrument": "EPIC", "timestamp": observed.isoformat(), "primary_action_state": "NO_TRADE", "1H_regime": "TREND", "model_version": "test"}})


def test_dashboard_complete_and_missing_markets_are_explicit(tmp_path):
    database = Database(tmp_path / "web.sqlite3")
    _seed_market(database)
    result = WebReadModel(database).dashboard()
    assert [item["market"] for item in result["markets"]] == ["US Tech 100", "Japan 225", "Hong Kong HS50"]
    us = result["markets"][0]
    assert us["trend_1h"] == "BULLISH"
    assert us["regime"] == "TREND"
    assert us["entry_quality"] == "NOT AVAILABLE"
    assert result["markets"][1]["price"] == "UNKNOWN"
    database.close()


def test_stale_market_is_not_reported_as_live(tmp_path):
    database = Database(tmp_path / "stale.sqlite3")
    _seed_market(database, stale=True)
    market = WebReadModel(database).dashboard()["markets"][0]
    assert market["freshness"] == "STALE"
    assert market["monitoring_state"] == "STALE_DATA"
    database.close()


def test_alert_ui_read_state_is_separate_and_survives_restart(tmp_path):
    path = tmp_path / "alerts.sqlite3"
    database = Database(path)
    alert = {"alert_id": "alert-1", "instrument": "US Tech 100", "alert_type": "DIRECTION_SHIFT", "priority": "WATCH", "event_identity": "event-1", "created_at": "2026-01-01T00:00:00+00:00", "confirmed": True, "message": "technical state changed", "alert_engine_version": "test"}
    assert database.save_alert(alert)
    assert WebReadModel(database).alerts()["alerts"][0]["is_read"] is False
    assert database.set_alert_read("alert-1")
    database.close()
    reopened = Database(path)
    row = WebReadModel(reopened).alerts(state="READ")["alerts"][0]
    assert row["is_read"] is True and row["read_at"]
    analytical = json.loads(reopened.connection.execute("SELECT payload_json FROM alerts WHERE alert_id='alert-1'").fetchone()[0])
    assert "read_at" not in analytical
    reopened.close()


def test_web_contract_has_no_trading_mutation_and_pwa_offline_safety():
    source = (Path(__file__).parents[1] / "src/ig_ai/web.py").read_text()
    js = (WEB_DIR / "assets/app.js").read_text()
    sw = (WEB_DIR / "assets/sw.js").read_text()
    manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text())
    assert "/api/orders" not in source.lower() and "/api/deals" not in source.lower()
    assert "OFFLINE / DATA UNAVAILABLE" in js
    assert "/api/" in sw and "cached" in sw
    assert manifest["name"] == "IG AI" and manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"]}
    assert {"/icons/maskable-192.svg", "/icons/maskable-512.svg"} <= {icon["src"] for icon in manifest["icons"]}
