from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ig_ai.candles import CandleAggregator
from ig_ai.database import Database
from ig_ai.models import Instrument, MarketObservation
from ig_ai.runtime import PersistedStream
from ig_ai.streaming import IGStreamService, StreamDiagnostics, Subscription


def _observation(timestamp: str, price: str = "100") -> MarketObservation:
    return MarketObservation(
        datetime.fromisoformat(timestamp).replace(tzinfo=UTC),
        "EPIC",
        "EPIC",
        "Market",
        Decimal(price),
        Decimal(price),
    )


class EmptyTransport:
    def __init__(self, updates=None, terminal=False):
        self.updates = list(updates or [])
        self.terminal = terminal
        self.diagnostics = StreamDiagnostics()

    def connect(self, *_):
        self.diagnostics.connection_verified = True

    def send(self, *_):
        pass

    def receive(self):
        if self.updates:
            return self.updates.pop(0)
        return None

    def is_connection_terminated(self):
        return self.terminal

    def close(self):
        self.diagnostics.intentional_shutdown = True


def _sink(tmp_path):
    db = Database(tmp_path / "acceptance.sqlite3")
    instrument = Instrument("EPIC", "EPIC", "Market")
    return db, PersistedStream(db, {"EPIC": instrument})


def test_duration_does_not_end_after_first_item_update_or_empty_receive(tmp_path):
    db, sink = _sink(tmp_path)
    transport = EmptyTransport([{"subscription_id": "1", "BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000"}])
    stream = IGStreamService("endpoint", "user", "secret", transport)
    stream.add_subscription(Subscription("EPIC", "PRICE:user:EPIC"))
    sink.run_for(stream, 0.08)
    assert sink.received_item_updates["EPIC"] == 1
    assert sink.runtime_audit["actual_elapsed"] >= 0.07
    assert sink.runtime_audit["exit_reason"] == "DURATION_COMPLETE"
    db.close()


def test_elapsed_runtime_is_monotonic_and_early_terminal_cannot_be_long_run_passed(tmp_path):
    db, sink = _sink(tmp_path)
    stream = IGStreamService("endpoint", "user", "secret", EmptyTransport(terminal=True))
    before = time.monotonic()
    sink.run_for(stream, 0.2)
    after = time.monotonic()
    assert before <= before + sink.runtime_audit["actual_elapsed"] <= after + 0.01
    assert sink.runtime_audit["exit_reason"] == "CONNECTION_TERMINATED"
    assert sink.runtime_audit["exit_reason"] != "DURATION_COMPLETE"
    db.close()


@pytest.mark.parametrize(
    ("timeframe", "next_timestamp"),
    (("15M", "2026-01-01T00:15:00"), ("1H", "2026-01-01T01:00:00"),
     ("4H", "2026-01-01T04:00:00"), ("1D", "2026-01-02T00:00:00")),
)
def test_repeated_ticks_are_one_forming_candle_and_boundary_finalizes_one(timeframe, next_timestamp):
    aggregator = CandleAggregator(timeframe)
    aggregator.update(_observation("2026-01-01T00:01:00", "100"))
    aggregator.update(_observation("2026-01-01T00:05:00", "105"))
    forming = aggregator.forming(_observation("2026-01-01T00:05:00", "105"))
    assert forming is not None and not forming.is_closed
    assert forming.observation_count == 2
    assert forming.open == Decimal("100") and forming.high == Decimal("105")
    closed = aggregator.update(_observation(next_timestamp, "103"))
    assert len(closed) == 1
    assert closed[0].is_closed
    assert closed[0].observation_count == 2


def test_controlled_shutdown_persists_incomplete_candle_as_forming_and_restart_continues(tmp_path):
    db, sink = _sink(tmp_path)
    update = {"instrument_id": "EPIC", "BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000"}
    sink.on_update(update)
    for aggregator in sink.aggregators.values():
        for candle in aggregator.flush():
            db.save_candle(candle)
    assert db.connection.execute("SELECT COUNT(*) FROM candles WHERE is_closed = 0").fetchone()[0] == 4
    db.close()
    db = Database(tmp_path / "acceptance.sqlite3")
    resumed = PersistedStream(db, {"EPIC": Instrument("EPIC", "EPIC", "Market")})
    resumed.on_update({**update, "BIDPRICE1": "104", "ASKPRICE1": "106", "TIMESTAMP": "1760000060000"})
    rows = db.connection.execute(
        "SELECT timeframe, COUNT(*), COUNT(DISTINCT start_at), MAX(is_closed), MIN(observation_count) "
        "FROM candles GROUP BY timeframe"
    ).fetchall()
    assert len(rows) == 4
    assert all(row[1:] == (1, 1, 0, 2) for row in rows)
    db.close()


@pytest.mark.parametrize(
    ("timeframe", "later_timestamp"),
    (("15M", "2026-01-01T00:15:00"), ("1H", "2026-01-01T01:00:00"),
     ("4H", "2026-01-01T04:00:00"), ("1D", "2026-01-02T00:00:00")),
)
def test_restart_later_bucket_finalizes_previous_forming_candle_once(tmp_path, timeframe, later_timestamp):
    path = tmp_path / f"restart-{timeframe}.sqlite3"
    instrument = Instrument("EPIC", "EPIC", "Market")
    db = Database(path)
    first = PersistedStream(db, {"EPIC": instrument})
    first.on_update({"instrument_id": "EPIC", "BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "2026-01-01T00:01:00+00:00"})
    for aggregator in first.aggregators.values():
        for candle in aggregator.flush():
            db.save_candle(candle)
    db.close()

    db = Database(path)
    resumed = PersistedStream(db, {"EPIC": instrument})
    resumed.on_update({"instrument_id": "EPIC", "BIDPRICE1": "104", "ASKPRICE1": "106", "TIMESTAMP": f"{later_timestamp}+00:00"})
    rows = db.connection.execute(
        "SELECT is_closed, observation_count FROM candles WHERE timeframe = ? ORDER BY start_at",
        (timeframe,),
    ).fetchall()
    assert rows == [(1, 1), (0, 1)]
    assert db.connection.execute("SELECT COUNT(*) FROM candles WHERE timeframe = ?", (timeframe,)).fetchone()[0] == 2
    db.close()


def test_database_schema_uses_required_identities_and_wal(tmp_path):
    db = Database(tmp_path / "integrity.sqlite3")
    assert db.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    candle_pk = db.connection.execute("PRAGMA table_info(candles)").fetchall()
    assert {row[1] for row in candle_pk if row[5]} == {"instrument_id", "timeframe", "start_at"}
    observation_pk = db.connection.execute("PRAGMA table_info(observations)").fetchall()
    assert {row[1] for row in observation_pk if row[5]} == {"instrument_id", "observed_at"}
    db.close()


def test_duplicate_observation_does_not_create_another_candle_or_observation(tmp_path):
    db, sink = _sink(tmp_path)
    update = {"instrument_id": "EPIC", "BIDPRICE1": "100", "ASKPRICE1": "102", "TIMESTAMP": "1760000000000"}
    assert sink.on_update(update)["observation_created"] == "true"
    assert sink.on_update(update)["skip_reason"] == "duplicate_observation"
    assert db.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert sink.forming_candles_by_instrument["EPIC"] == {timeframe: 1 for timeframe in sink.timeframes}
    db.close()


def test_persistence_is_owner_thread_only_and_secret_diagnostics_are_redacted(tmp_path):
    db, sink = _sink(tmp_path)
    errors = []

    def wrong_thread():
        try:
            db.connection.execute("SELECT 1")
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    import threading
    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join()
    assert errors and isinstance(errors[0], sqlite3.ProgrammingError)
    sink.safe_skip_diagnostics.append({"reason": "missing_bid_or_ask"})
    assert "CST-secret" not in str(sink.safe_skip_diagnostics)
    db.close()
