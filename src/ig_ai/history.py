"""Read-only IG CFD history ingestion and point-in-time replay.

The provider-specific contract is deliberately kept here.  Historical prices
are constructed from the arithmetic mid of each bid/ask OHLC field; volume is
never inferred or stored.  ``snapshotTimeUTC`` is preferred, with an explicit
UTC fallback for fixtures/providers that omit it.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from math import isfinite
from typing import Any

from .direction import DIRECTION_SCHEMA_VERSION
from .discovery import discover_market_groups, select_stream_instruments
from .exceptions import IGHTTPError, MalformedResponseError, RateLimitError
from .models import Candle
from .patterns import PATTERN_SCHEMA_VERSION, Phase2BEngine
from .research import HistoricalReplay, ResearchEngine
from .technical import FEATURE_SCHEMA_VERSION

HISTORY_SCHEMA_VERSION = "history_v1"
REPLAY_SCHEMA_VERSION = "replay_v1"
SUPPORTED_TIMEFRAMES = ("15M", "1H", "4H", "1D")
IG_RESOLUTIONS = {"15M": "MINUTE_15", "1H": "HOUR", "4H": "HOUR_4", "1D": "DAY"}
TIMEFRAME_DURATION = {"15M": timedelta(minutes=15), "1H": timedelta(hours=1), "4H": timedelta(hours=4), "1D": timedelta(days=1)}
HORIZONS = ("15M", "1H", "2H", "4H", "8H", "1D")
ALLOWANCE_CODES = {"error.public-api.exceeded-api-key-allowance", "error.public-api.exceeded-account-allowance", "error.public-api.exceeded-account-historical-data-allowance"}


def parse_ig_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MalformedResponseError("historical candle timestamp is missing")
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
        except ValueError:
            continue
    raise MalformedResponseError("historical candle timestamp is invalid")


def _price(value: Any) -> Decimal:
    if isinstance(value, dict):
        bid, ask = value.get("bid"), value.get("ask", value.get("offer"))
        if bid is not None and ask is not None:
            return (Decimal(str(bid)) + Decimal(str(ask))) / Decimal(2)
        value = value.get("lastTraded") or value.get("lastTradedPrice")
    if value is None:
        raise InvalidOperation
    return Decimal(str(value))


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def normalize_historical_price(row: dict[str, Any], *, instrument_id: str, epic: str, timeframe: str) -> Candle:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    timestamp = _field(row, "snapshotTimeUTC", "snapshotTime", "timestamp", "time")
    start = parse_ig_timestamp(timestamp)
    end = start + TIMEFRAME_DURATION[timeframe]
    opens = _field(row, "openPrice", "open")
    highs = _field(row, "highPrice", "high")
    lows = _field(row, "lowPrice", "low")
    closes = _field(row, "closePrice", "close")
    try:
        candle = Candle(instrument_id, epic, timeframe, start, end, _price(opens), _price(highs), _price(lows), _price(closes), volume=None, is_closed=True)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MalformedResponseError("historical OHLC is incomplete or non-numeric") from exc
    validate_candle(candle)
    return candle


def validate_candle(candle: Candle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close)
    if any(not isfinite(float(value)) for value in values):
        raise ValueError("historical OHLC must be finite")
    if candle.end <= candle.start or candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close) or candle.high < candle.low:
        raise ValueError("historical OHLC or interval is invalid")
    if candle.end - candle.start != TIMEFRAME_DURATION[candle.timeframe]:
        raise ValueError("historical candle duration is invalid")
    if not candle.is_closed or not candle.instrument_id or not candle.epic:
        raise ValueError("historical candle identity/closed state is invalid")


def aggregate_candles(candles: Iterable[Candle], timeframe: str = "4H") -> list[Candle]:
    if timeframe != "4H":
        raise ValueError("only deterministic 4H aggregation is supported")
    source = sorted((c for c in candles if c.is_closed), key=lambda c: c.start)
    groups: dict[datetime, list[Candle]] = {}
    for candle in source:
        anchor = candle.start.replace(hour=(candle.start.hour // 4) * 4, minute=0, second=0, microsecond=0)
        groups.setdefault(anchor, []).append(candle)
    result = []
    for start, items in groups.items():
        if not items or any(item.end <= item.start for item in items):
            continue
        result.append(Candle(items[0].instrument_id, items[0].epic, "4H", start, start + timedelta(hours=4), items[0].open, max(i.high for i in items), min(i.low for i in items), items[-1].close, is_closed=True))
    return result


@dataclass(frozen=True)
class GapReport:
    missing_intervals: int
    largest_gap: timedelta | None
    uncertain_session_gaps: int


def report_gaps(candles: Iterable[Candle]) -> GapReport:
    rows = sorted(candles, key=lambda c: c.start)
    if len(rows) < 2:
        return GapReport(0, None, 0)
    duration = TIMEFRAME_DURATION[rows[0].timeframe]
    gaps = [current.start - previous.end for previous, current in pairwise(rows) if current.start > previous.end]
    return GapReport(sum(max(0, int(gap / duration) - 1) for gap in gaps), max(gaps, default=None), len(gaps))


def _request_plan(start: datetime, end: datetime, timeframe: str, chunk_days: int) -> list[tuple[datetime, datetime]]:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be timezone-aware and end must be after start")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    step = timedelta(days=chunk_days)
    result, current = [], start.astimezone(UTC)
    end = end.astimezone(UTC)
    while current < end:
        next_end = min(current + step, end)
        result.append((current, next_end))
        current = next_end
    return result


class HistoricalIGClient:
    """Small adapter around the existing read-only IG REST client."""

    def __init__(self, client, *, pacing_seconds: float = 1.0, max_retries: int = 2, backoff_seconds: float = 1.0):
        self.client = client
        self.pacing_seconds = max(0.0, pacing_seconds)
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def fetch(self, epic: str, timeframe: str, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        resolution = IG_RESOLUTIONS[timeframe]
        last_meta: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            try:
                payload, headers = self.client._request("GET", f"/prices/{epic}/{resolution}", params={"startdate": start.isoformat(), "enddate": end.isoformat()}, version="3", retry=False, phase="historical_backfill")
                rows = payload.get("prices", [])
                if not isinstance(rows, list):
                    raise MalformedResponseError("IG historical response has invalid prices")
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                last_meta = {"resolution": resolution, "rows": len(rows), "headers": {key: value for key, value in headers.items() if key.lower() in {"x-request-id", "allowance-remaining", "allowance-limit", "allowance-reset"}}, "metadata": metadata}
                return rows, last_meta
            except RateLimitError:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(self.backoff_seconds * (2 ** attempt), 30.0))
            except IGHTTPError as exc:
                if exc.provider_code in ALLOWANCE_CODES or exc.status == 429:
                    raise RateLimitError(exc.status, "historical allowance exhausted", retryable=True, provider_code=exc.provider_code, endpoint=exc.endpoint, method=exc.method, phase=exc.phase) from exc
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                time.sleep(min(self.backoff_seconds * (2 ** attempt), 30.0))
        raise RuntimeError("unreachable historical request state")


class BackfillService:
    def __init__(self, database, historical_client: HistoricalIGClient):
        self.database, self.client = database, historical_client

    def resolve(self, market: str):
        groups = discover_market_groups(self.client.client)
        group = next((item for item in groups if item.requested_market == market), None)
        if group is None:
            raise ValueError(f"unsupported market: {market}")
        selected = [item for item in select_stream_instruments([group]) if item.requested_market == market]
        if not selected:
            raise RuntimeError(f"no provider-verified weekday cash/rolling CFD for {market}")
        return selected[0]

    def run(self, *, market: str, timeframe: str, start: datetime, end: datetime, dry_run: bool = False, resume: bool = False, job_id: str | None = None, chunk_days: int = 30) -> dict[str, Any]:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        candidate = self.resolve(market)
        instrument_id = f"ig:{candidate.epic}"
        plan = _request_plan(start, end, timeframe, chunk_days)
        if dry_run:
            return {"status": "DRY_RUN", "market": market, "epic": candidate.epic, "timeframe": timeframe, "chunks": [(a.isoformat(), b.isoformat()) for a, b in plan]}
        self.database.save_instrument(instrument_id, candidate.epic, candidate.market_name, instrument_type=candidate.instrument_type, market_status=candidate.market_status, metadata={**candidate.metadata, "provider": "IG", "asset_source_type": "CFD", "requested_market": market, "classification": candidate.classification})
        identity = job_id or self.database.deterministic_backfill_job_id(instrument_id, timeframe, start.isoformat(), end.isoformat())
        prior = self.database.get_backfill_job(identity) if resume else None
        self.database.start_backfill_job(identity, instrument_id, candidate.epic, market, timeframe, start.isoformat(), end.isoformat())
        checkpoint = parse_ig_timestamp(prior["current_progress"]) if prior and prior.get("current_progress") else None
        if checkpoint:
            plan = [(chunk_start, chunk_end) for chunk_start, chunk_end in plan if chunk_end > checkpoint]
        retrieved = int(prior.get("rows_retrieved", 0)) if prior else 0
        inserted = int(prior.get("rows_inserted", 0)) if prior else 0
        skipped = int(prior.get("rows_skipped", 0)) if prior else 0
        try:
            for chunk_start, chunk_end in plan:
                rows, metadata = self.client.fetch(candidate.epic, timeframe, chunk_start, chunk_end)
                chunk_inserted = 0
                for row in sorted(rows, key=lambda value: str(value.get("snapshotTimeUTC") or value.get("snapshotTime") or "")):
                    candle = normalize_historical_price(row, instrument_id=instrument_id, epic=candidate.epic, timeframe=timeframe)
                    if not (start <= candle.start < end):
                        continue
                    retrieved += 1
                    result = self.database.save_historical_candle(candle, provenance={"provider": "IG", "asset_source_type": "CFD", "market_name": market, "epic": candidate.epic, "provider_instrument_name": candidate.market_name, "currency": candidate.metadata.get("currency") or candidate.metadata.get("currencyCode"), "contract_metadata": candidate.metadata, "resolution": IG_RESOLUTIONS[timeframe], "retrieved_at": datetime.now(UTC).isoformat()})
                    inserted += result == "INSERTED"
                    skipped += result != "INSERTED"
                    chunk_inserted += result == "INSERTED"
                self.database.checkpoint_backfill_job(identity, chunk_end.isoformat(), retrieved, inserted, skipped, metadata)
                if self.client.pacing_seconds:
                    time.sleep(self.client.pacing_seconds)
            self.database.finish_backfill_job(identity, "COMPLETE", retrieved, inserted, skipped)
            return {"status": "COMPLETE", "job_id": identity, "market": market, "instrument_id": instrument_id, "epic": candidate.epic, "timeframe": timeframe, "retrieved": retrieved, "inserted": inserted, "skipped": skipped, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "available_range": self.database.available_history(instrument_id, timeframe)}
        except RateLimitError:
            self.database.finish_backfill_job(identity, "PAUSED", retrieved, inserted, skipped)
            return {"status": "BACKFILL_PAUSED_RATE_LIMIT", "job_id": identity, "retrieved": retrieved, "inserted": inserted, "skipped": skipped}
        except Exception:
            self.database.finish_backfill_job(identity, "FAILED", retrieved, inserted, skipped)
            raise


class ReplayService:
    def __init__(self, database, *, max_history: int = 600):
        self.database, self.max_history = database, max_history

    def run(self, *, instrument_id: str, start: datetime, end: datetime, window: str = "3Y") -> dict[str, Any]:
        timeframes = {tf: self.database.list_candles(instrument_id, tf) for tf in SUPPORTED_TIMEFRAMES}
        job_id = self.database.deterministic_replay_job_id(instrument_id, start.isoformat(), end.isoformat(), window)
        self.database.start_replay_job(job_id, instrument_id, start.isoformat(), end.isoformat(), window)
        replay = HistoricalReplay(max_history=self.max_history)
        engine = ResearchEngine(self.database)
        phase2b = Phase2BEngine()
        processed = snapshots = outcomes = 0
        prior = self.database.get_replay_job(job_id)
        checkpoint = parse_ig_timestamp(prior["last_information_time"]) if prior and prior.get("last_information_time") else None
        for item in replay.direction_states(timeframes):
            target = item["reference_candle"]
            if not (start <= target.start < end):
                continue
            if checkpoint and target.end <= checkpoint:
                continue
            state = item["state"]
            state["feature_schema_version"] = FEATURE_SCHEMA_VERSION
            state["pattern_schema_version"] = PATTERN_SCHEMA_VERSION
            state["direction_schema_version"] = DIRECTION_SCHEMA_VERSION
            for _timeframe, candles in item["histories"].items():
                if not candles:
                    continue
                analysis = phase2b.analyze(candles)
                features = phase2b.technical.calculate(candles)
                self.database.save_technical_features(features)
                self.database.save_phase2b_analysis(analysis)
            state["model_reference"]["information_time"] = target.end.isoformat()
            snapshot_id = engine.record_snapshot(instrument_id=instrument_id, timeframe="1H", snapshot_timestamp=target.start.isoformat(), reference_time=target.end.isoformat(), reference_price=float(target.close), snapshot_type="DIRECTION_REPLAY", state=state, source_identity=self.database.research_source_identity(instrument_id))
            outcomes += engine.complete_snapshot(snapshot_id, reference_time=target.end, reference_price=float(target.close), direction=state.get("direction"), candles=timeframes["15M"], horizons=HORIZONS)
            processed += 1
            snapshots += 1
            self.database.checkpoint_replay_job(job_id, target.end.isoformat(), snapshots, outcomes)
        self.database.finish_replay_job(job_id, "COMPLETE", processed, snapshots, outcomes)
        return {"status": "COMPLETE", "job_id": job_id, "processed": processed, "snapshots": snapshots, "outcomes": outcomes}
