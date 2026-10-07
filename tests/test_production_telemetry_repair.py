from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ig_ai.database import Database
from ig_ai.market_identity import CANONICAL_MARKETS, canonical_instrument_rows
from ig_ai.models import Candle, Instrument
from ig_ai.runtime import PersistedStream
from ig_ai.runtime_service import RuntimeService, Schedule
from ig_ai.web import WebReadModel


def _candle(instrument_id: str, timeframe: str, start: str, *, closed: bool) -> Candle:
    begin = datetime.fromisoformat(start).replace(tzinfo=UTC)
    minutes = {"15M": 15, "1H": 60}[timeframe]
    from datetime import timedelta

    return Candle(
        instrument_id,
        instrument_id,
        timeframe,
        begin,
        begin + timedelta(minutes=minutes),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        is_closed=closed,
        observation_count=1,
    )


def _decision(instrument_id: str, timestamp: str, action: str = "NO_TRADE") -> dict:
    return {
        "instrument": instrument_id,
        "reference_time": timestamp,
        "primary_action_state": action,
        "telemetry": {
            "instrument": instrument_id,
            "timestamp": timestamp,
            "primary_action_state": action,
            "market_bias": "BULLISH",
            "evaluated_side": "LONG",
            "1H_regime": "TREND_UP",
            "model_version": "test",
        },
    }


def _alert(instrument_id: str, created_at: str) -> dict:
    return {
        "alert_id": f"alert-{instrument_id}",
        "instrument": instrument_id,
        "alert_type": "DIRECTION_SHIFT",
        "priority": "INFO",
        "event_identity": f"event-{instrument_id}",
        "created_at": created_at,
        "confirmed": True,
        "message": "test alert",
        "alert_engine_version": "test",
    }


def test_all_configured_market_labels_resolve_to_canonical_ids():
    rows = [
        ("US-ID", "美國科技股100 現貨 ($1)", "OPEN", "US-EPIC", "{}"),
        ("JP-ID", "日本225 現貨 ($1)", "OPEN", "JP-EPIC", "{}"),
        ("HK-ID", "香港HS50 現貨 ($1)", "OPEN", "HK-EPIC", "{}"),
    ]

    resolved = canonical_instrument_rows(rows)

    assert tuple(resolved) == CANONICAL_MARKETS
    assert [resolved[label][0] for label in CANONICAL_MARKETS] == ["US-ID", "JP-ID", "HK-ID"]


def test_dashboard_exposes_genuine_closed_telemetry_without_cross_market_leakage(tmp_path):
    database = Database(tmp_path / "telemetry.sqlite3")
    database.save_instrument("US-ID", "US-EPIC", "美國科技股100 現貨 ($1)", market_status="OPEN")
    database.save_instrument("JP-ID", "JP-EPIC", "日本225 現貨 ($1)", market_status="OPEN")
    database.save_observation(
        __import__("ig_ai.models", fromlist=["MarketObservation"]).MarketObservation(
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "US-ID", "US-EPIC", "美國科技股100 現貨 ($1)", Decimal("100"), Decimal("101")
        )
    )
    database.save_observation(
        __import__("ig_ai.models", fromlist=["MarketObservation"]).MarketObservation(
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "JP-ID", "JP-EPIC", "日本225 現貨 ($1)", Decimal("200"), Decimal("201")
        )
    )
    for instrument_id in ("US-ID", "JP-ID"):
        database.save_candle(_candle(instrument_id, "15M", "2026-01-01T11:45:00+00:00", closed=True))
        database.save_candle(_candle(instrument_id, "15M", "2026-01-01T12:00:00+00:00", closed=False))
        database.save_candle(_candle(instrument_id, "1H", "2026-01-01T11:00:00+00:00", closed=True))
        database.save_candle(_candle(instrument_id, "1H", "2026-01-01T12:00:00+00:00", closed=False))
    database.save_decision_telemetry(_decision("US-ID", "2026-01-01T11:00:00+00:00", "US_ACTION"))
    database.save_decision_telemetry(_decision("JP-ID", "2026-01-01T11:00:00+00:00", "JP_ACTION"))
    database.save_alert(_alert("US-ID", "2026-01-01T12:01:00+00:00"))

    markets = {item["market"]: item for item in WebReadModel(database).dashboard()["markets"]}

    assert markets["US Tech 100"]["price"] == "100.5"
    assert markets["US Tech 100"]["last_closed_15m_at"] == "2026-01-01T11:45:00+00:00"
    assert markets["US Tech 100"]["last_closed_1h_at"] == "2026-01-01T11:00:00+00:00"
    assert markets["US Tech 100"]["last_decision_at"] == "2026-01-01T11:00:00+00:00"
    assert markets["US Tech 100"]["last_alert_at"] == "2026-01-01T12:01:00+00:00"
    assert markets["Japan 225"]["price"] == "200.5"
    assert markets["Japan 225"]["primary_action"] == "JP_ACTION"
    assert markets["Japan 225"]["last_alert_at"] is None
    assert markets["Hong Kong HS50"]["last_closed_15m_at"] is None
    database.close()


def test_runtime_health_exposes_persisted_telemetry_and_preserves_no_alert(tmp_path):
    database = Database(tmp_path / "runtime-telemetry.sqlite3")
    database.save_instrument("EPIC", "EPIC", "US Tech 100", market_status="OPEN")
    database.save_candle(_candle("EPIC", "15M", "2026-01-01T11:45:00+00:00", closed=True))
    database.save_candle(_candle("EPIC", "1H", "2026-01-01T11:00:00+00:00", closed=True))
    database.save_decision_telemetry(_decision("EPIC", "2026-01-01T11:00:00+00:00"))
    service = RuntimeService(database)
    service.update_market("EPIC", schedule=Schedule("24_7"), last_data=datetime.now(UTC), connection="CONNECTED")

    state = database.list_runtime_market_states()[0]
    assert state["last_closed_15m_at"] == "2026-01-01T11:45:00+00:00"
    assert state["last_closed_1h_at"] == "2026-01-01T11:00:00+00:00"
    assert state["last_decision_at"] == "2026-01-01T11:00:00+00:00"
    assert state["last_alert_at"] is None
    database.close()


def test_live_decision_helper_persists_only_the_engine_result(tmp_path, monkeypatch):
    database = Database(tmp_path / "live-decision.sqlite3")
    database.save_instrument("EPIC", "EPIC", "US Tech 100", market_status="OPEN")
    sink = PersistedStream(database, {"EPIC": Instrument("EPIC", "EPIC", "US Tech 100")})
    decision = _decision("EPIC", "2026-01-01T11:00:00+00:00", "LIVE_ACTION")

    class FakeEngine:
        def analyze(self, state):
            assert state == {"genuine": True}
            return decision

    monkeypatch.setattr("ig_ai.runtime.normalized_state_from_database", lambda *_: {"genuine": True})
    monkeypatch.setattr("ig_ai.runtime.TradingDecisionEngine", FakeEngine)
    sink._persist_live_decision("EPIC")

    assert database.get_decision_telemetry("EPIC", limit=1) == [decision["telemetry"]]
    database.close()
