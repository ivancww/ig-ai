import json
import struct
import threading
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

from ig_ai.database import Database
from ig_ai.direction import DIRECTION_SCHEMA_VERSION
from ig_ai.web import WEB_DIR, WebHandler, WebReadModel, _resolve_web_dir


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


def test_dashboard_surfaces_persisted_phase8b_decision_telemetry(tmp_path):
    database = Database(tmp_path / "phase8b.sqlite3")
    _seed_market(database)
    database.save_decision_telemetry({
        "instrument": "EPIC",
        "reference_time": "2026-10-05T10:00:00+00:00",
        "primary_action_state": "ENTRY_QUALIFIED",
        "telemetry": {
            "instrument": "EPIC", "timestamp": "2099-10-05T10:00:00+00:00",
            "market_bias": "BULLISH", "evaluated_side": "LONG", "1H_regime": "TREND_UP",
            "entry_quality_score": 85, "entry_quality_qualifier": "ENTRY_QUALIFIED", "model_version": "trading_decision_v1",
            "primary_action_state": "ENTRY_QUALIFIED",
            "structure_invalidation_state": "ORIGINAL_STRUCTURE_VALID",
            "profit_protection_state": "NORMAL",
            "big_wave_qualification_state": "RUNNER_ELIGIBLE",
        },
    })
    market = WebReadModel(database).dashboard()["markets"][0]
    assert market["regime"] == "TREND_UP"
    assert market["market_bias"] == "BULLISH"
    assert market["evaluated_side"] == "LONG"
    assert market["entry_quality"] == 85
    assert market["entry_quality_state"] == "ENTRY_QUALIFIED"
    assert market["primary_action"] == "ENTRY_QUALIFIED"
    assert market["structure_invalidation"] == "ORIGINAL_STRUCTURE_VALID"
    assert market["profit_protection"] == "NORMAL"
    assert market["runner_state"] == "RUNNER_ELIGIBLE"
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


def test_alert_filter_resolves_display_market_to_persisted_instrument_id(tmp_path):
    database = Database(tmp_path / "identity.sqlite3")
    database.save_instrument("EPIC.NDX", "IX.D.NASDAQ.IFMM.IP", "US Tech 100", market_status="OPEN")
    alert = {
        "alert_id": "real-id-alert", "instrument": "EPIC.NDX", "alert_type": "DIRECTION_SHIFT",
        "priority": "WATCH", "event_identity": "real-id-event", "created_at": "2026-01-01T00:00:00+00:00",
        "confirmed": True, "message": "identity test", "alert_engine_version": "test",
    }
    assert database.save_alert(alert)
    result = WebReadModel(database).alerts(market="US Tech 100")
    assert [item["alert_id"] for item in result["alerts"]] == ["real-id-alert"]
    assert result["alerts"][0]["market"] == "US Tech 100"
    assert "instrument" not in result["alerts"][0]
    database.close()


def test_web_contract_has_no_trading_mutation_and_pwa_offline_safety():
    source = (Path(__file__).parents[1] / "src/ig_ai/web.py").read_text()
    js = (WEB_DIR / "assets/app.js").read_text()
    sw = (WEB_DIR / "assets/sw.js").read_text()
    manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text())
    index = (WEB_DIR / "index.html").read_text()
    css = (WEB_DIR / "assets/app.css").read_text()
    icon_contract = (WEB_DIR / "icons/README.md").read_text()
    assert "/api/orders" not in source.lower() and "/api/deals" not in source.lower()
    assert "OFFLINE / DATA UNAVAILABLE" in js
    assert "/api/" in sw and "cached" in sw
    assert manifest["name"] == "IG AI" and manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"]}
    assert {"/icons/maskable-192.png", "/icons/maskable-512.png"} <= {icon["src"] for icon in manifest["icons"]}
    assert all(icon["type"] == "image/png" for icon in manifest["icons"])
    assert all((WEB_DIR / icon["src"].lstrip("/")).is_file() for icon in manifest["icons"])
    assert '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">' in index
    assert '<link rel="icon" href="/icons/favicon.png" type="image/png">' in index
    assert "/icons/favicon.svg" not in index
    assert 'class="brand-logo" src="/icons/icon-192.png"' in index
    assert "↗" not in index and ".brand-logo" in css
    assert "approved production" in icon_contract.lower()
    assert "placeholders" not in icon_contract.lower()
    production_assets = {
        "icon-192.png": (192, 192), "icon-512.png": (512, 512),
        "maskable-192.png": (192, 192), "maskable-512.png": (512, 512),
        "apple-touch-icon.png": (180, 180), "favicon.png": (32, 32),
    }
    assert all((WEB_DIR / "icons" / name).is_file() for name in production_assets)
    for name, size in production_assets.items():
        png = (WEB_DIR / "icons" / name).read_bytes()
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == size
    assert not any(icon["src"].endswith(".svg") for icon in manifest["icons"])
    for icon in manifest["icons"]:
        png = (WEB_DIR / icon["src"].lstrip("/")).read_bytes()
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == tuple(int(size) for size in icon["sizes"].split("x"))


def test_installed_package_declares_and_resolves_web_assets(tmp_path):
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    assert data_files["share/ig-ai/web"] == ["web/index.html", "web/manifest.webmanifest"]
    assert data_files["share/ig-ai/web/assets"] == ["web/assets/*"]
    assert data_files["share/ig-ai/web/icons"] == ["web/icons/*"]

    prefix = tmp_path / "venv"
    installed_web = prefix / "share" / "ig-ai" / "web"
    installed_web.mkdir(parents=True)
    module_path = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "ig_ai" / "web.py"
    assert _resolve_web_dir(module_path, str(prefix)) == installed_web


def test_source_checkout_web_assets_remain_the_fallback(tmp_path):
    module_path = tmp_path / "checkout" / "src" / "ig_ai" / "web.py"
    expected = tmp_path / "checkout" / "web"
    assert _resolve_web_dir(module_path, str(tmp_path / "missing-prefix")) == expected


def test_responsive_layout_contract_covers_folded_and_unfolded_viewports():
    css = (WEB_DIR / "assets/app.css").read_text()
    assert "grid-template-columns:1fr" in css
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in css
    assert "overflow-x:hidden" in css
    assert "min-height:48px" in css
    assert "position:fixed" in css and "top:64px" in css
    assert "device" not in css.lower()


def test_static_path_traversal_is_not_resolvable():
    source = (Path(__file__).parents[1] / "src/ig_ai/web.py").read_text()
    assert "resolve()" in source and "WEB_DIR not in path.parents" in source


def test_http_api_is_sanitized_no_store_and_has_no_mutation_routes(tmp_path):
    path = tmp_path / "http.sqlite3"
    database = Database(path)
    _seed_market(database)
    database.close()
    server = HTTPServer(("127.0.0.1", 0), WebHandler)

    ready = threading.Event()

    def run_server():
        owner_database = Database(path)
        server.model = WebReadModel(owner_database)
        ready.set()
        server.serve_forever()
        owner_database.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        response = urllib.request.urlopen(f"{base}/api/dashboard")
        payload = response.read().decode()
        assert response.headers["Cache-Control"] == "no-store"
        assert "IG_PASSWORD" not in payload and "X-SECURITY-TOKEN" not in payload and "EPIC" not in payload
        for path in ("/api/orders", "/api/deals", "/../pyproject.toml"):
            try:
                urllib.request.urlopen(base + path)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError(f"unsafe route unexpectedly succeeded: {path}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
