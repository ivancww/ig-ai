"""Read-only IG CFD history ingestion and point-in-time replay.

The provider-specific contract is deliberately kept here.  Historical prices
are constructed from the arithmetic mid of each bid/ask OHLC field; volume is
never inferred or stored.  ``snapshotTimeUTC`` is preferred, with an explicit
UTC fallback for fixtures/providers that omit it.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from math import isfinite
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

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
SAFE_CHUNK_DAYS = {"15M": 2, "1H": 30, "4H": 90, "1D": 180}
TIMEFRAME_DURATION = {"15M": timedelta(minutes=15), "1H": timedelta(hours=1), "4H": timedelta(hours=4), "1D": timedelta(days=1)}
HORIZONS = ("15M", "1H", "2H", "4H", "8H", "1D")
ALLOWANCE_CODES = {"error.public-api.exceeded-api-key-allowance", "error.public-api.exceeded-account-allowance", "error.public-api.exceeded-account-historical-data-allowance"}


def parse_ig_timestamp(value: Any, *, allow_naive: bool = False) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MalformedResponseError("historical candle timestamp is missing")
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None and not allow_naive:
                raise MalformedResponseError("un-zoned IG provider timestamp is not accepted; use snapshotTimeUTC")
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.strptime(text, "%Y:%m:%d-%H:%M:%S")
        if not allow_naive:
            raise MalformedResponseError("un-zoned IG provider timestamp is not accepted; use snapshotTimeUTC")
        return parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
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


def safe_historical_response_diagnostic(row: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe historical response shape without returning provider values."""
    timestamp_fields = {}
    for name in ("snapshotTimeUTC", "snapshotTime", "timestamp", "time"):
        if name not in row:
            continue
        value = row[name]
        item = {"type": type(value).__name__}
        if isinstance(value, str):
            item.update({"length": len(value), "masked_shape": re.sub(r"[A-Za-z0-9]", "X", value)})
        timestamp_fields[name] = item

    def field_types(value: Any) -> dict[str, str]:
        return {key: type(item).__name__ for key, item in value.items()} if isinstance(value, dict) else {}

    metadata = metadata if isinstance(metadata, dict) else {}
    metadata_names = {"allowance", "pageData", "paging", "size", "next"}
    return {
        "timestamp_fields": timestamp_fields,
        "row_fields": field_types(row),
        "metadata_fields": {key: type(metadata[key]).__name__ for key in sorted(metadata_names & metadata.keys())},
        "paging_fields": {
            key: field_types(metadata.get(key))
            for key in ("paging", "pageData")
            if isinstance(metadata.get(key), dict)
        },
    }


def _malformed_row_diagnostic(row: dict[str, Any], metadata: dict[str, Any] | None, chunk_start: datetime, chunk_end: datetime, exc: Exception) -> dict[str, Any]:
    """Return bounded, non-sensitive metadata for an unusable provider row."""
    timestamp = None
    for name, allow_naive in (("snapshotTimeUTC", False), ("snapshotTime", True), ("timestamp", False), ("time", False)):
        if row.get(name) is None:
            continue
        try:
            timestamp = parse_ig_timestamp(row[name], allow_naive=allow_naive).isoformat()
        except (MalformedResponseError, TypeError, ValueError):
            timestamp = None
        break
    reason = "historical_row_unusable"
    if isinstance(exc, ValueError):
        reason = "historical_row_ohlc_inconsistent_or_non_finite"
    return {
        "gap_type": "PROVIDER_MALFORMED_ROW_GAP",
        "timestamp": timestamp,
        "chunk_start": chunk_start.isoformat(),
        "chunk_end": chunk_end.isoformat(),
        "reason": reason,
        "response_shape": safe_historical_response_diagnostic(row, metadata),
    }


def normalize_historical_price(row: dict[str, Any], *, instrument_id: str, epic: str, timeframe: str) -> Candle:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    timestamp = _field(row, "snapshotTimeUTC")
    if timestamp is not None:
        # Prefer IG's authoritative UTC field whenever the provider supplies it.
        start = parse_ig_timestamp(timestamp)
    elif row.get("snapshotTime") is not None:
        # The verified v1 response documents snapshotTime without an offset;
        # IG's general API date semantics make that value UTC.  Do not apply
        # this interpretation to arbitrary fallback fields.
        start = parse_ig_timestamp(row["snapshotTime"], allow_naive=True)
    else:
        timestamp = _field(row, "timestamp", "time")
        if isinstance(timestamp, str) and ("+" not in timestamp and not timestamp.endswith("Z")):
            raise MalformedResponseError("historical timestamp fallback must include an explicit timezone")
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
        items = sorted(items, key=lambda item: item.start)
        expected_starts = [start + timedelta(hours=index) for index in range(4)]
        complete = len(items) == 4 and [item.start for item in items] == expected_starts and all(
            item.end == item.start + timedelta(hours=1) and item.is_closed for item in items
        )
        if not complete:
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
        self.request_count = 0

    def fetch(self, epic: str, timeframe: str, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        resolution = IG_RESOLUTIONS[timeframe]
        all_rows: list[dict[str, Any]] = []
        page_count = 0
        # IG's date-range query contract is version 1 and requires its
        # provider-specific yyyy:MM:dd-HH:mm:ss format.  The version-2 path
        # and version-3 /prices/{epic} forms are not accepted by the live
        # historical endpoint used by this account.
        start_text = start.astimezone(UTC).strftime("%Y:%m:%d-%H:%M:%S")
        end_text = end.astimezone(UTC).strftime("%Y:%m:%d-%H:%M:%S")
        path = f"/prices/{quote(epic, safe='')}/{resolution}"
        params: dict[str, str] | None = {"startdate": start_text, "enddate": end_text}
        while True:
            page_attempt = 0
            while True:
                try:
                    self.request_count += 1
                    payload, headers = self.client._request("GET", path, params=params, version="1", retry=False, phase="historical_backfill")
                    break
                except RateLimitError:
                    if page_attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.backoff_seconds * (2 ** page_attempt), 30.0))
                    page_attempt += 1
                except IGHTTPError as exc:
                    if exc.provider_code in ALLOWANCE_CODES or exc.status == 429:
                        raise RateLimitError(exc.status, "historical allowance exhausted", retryable=True, provider_code=exc.provider_code, endpoint=exc.endpoint, method=exc.method, phase=exc.phase) from exc
                    if not exc.retryable or page_attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.backoff_seconds * (2 ** page_attempt), 30.0))
                    page_attempt += 1

            rows = payload.get("prices", [])
            if not isinstance(rows, list):
                raise MalformedResponseError("IG historical response has invalid prices")
            all_rows.extend(row for row in rows if isinstance(row, dict))
            page_count += 1
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            paging = metadata.get("paging") if isinstance(metadata.get("paging"), dict) else {}
            next_url = paging.get("next") or metadata.get("next")
            if next_url:
                parsed = urlsplit(str(next_url))
                if not parsed.path.startswith("/prices/"):
                    raise MalformedResponseError("IG historical paging link is not a price endpoint")
                path = parsed.path
                params = dict(parse_qsl(parsed.query, keep_blank_values=True))
                continue
            page_data = metadata.get("pageData") if isinstance(metadata.get("pageData"), dict) else {}
            total_pages = page_data.get("totalPages") if page_data.get("totalPages") is not None else paging.get("totalPages")
            page_number = page_data.get("pageNumber") if page_data.get("pageNumber") is not None else paging.get("pageNumber")
            page_size = page_data.get("pageSize") if page_data.get("pageSize") is not None else paging.get("pageSize")
            if total_pages is not None and page_number is not None and int(page_number) + 1 < int(total_pages):
                raise MalformedResponseError("IG historical response is truncated: paging metadata has no next link")
            if page_size and len(rows) >= int(page_size):
                raise MalformedResponseError("IG historical response may be truncated at provider page size")
            return all_rows, {"resolution": resolution, "rows": len(all_rows), "pages": page_count, "complete": True, "headers": {key: value for key, value in headers.items() if key.lower() in {"x-request-id", "allowance-remaining", "allowance-limit", "allowance-reset"}}, "metadata": metadata}


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

    def run(self, *, market: str, timeframe: str, start: datetime, end: datetime, dry_run: bool = False, resume: bool = False, job_id: str | None = None, chunk_days: int | None = None) -> dict[str, Any]:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        candidate = self.resolve(market)
        instrument_id = f"ig:{candidate.epic}"
        plan = _request_plan(start, end, timeframe, chunk_days or SAFE_CHUNK_DAYS[timeframe])
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
        malformed_rows = int(prior.get("malformed_rows", 0)) if prior else 0
        skipped_malformed_rows = int(prior.get("skipped_malformed_rows", 0)) if prior else 0
        try:
            malformed_diagnostics = json.loads(prior.get("malformed_diagnostics_json", "[]")) if prior else []
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed_diagnostics = []
        if not isinstance(malformed_diagnostics, list):
            malformed_diagnostics = []
        try:
            for chunk_start, chunk_end in plan:
                rows, metadata = self.client.fetch(candidate.epic, timeframe, chunk_start, chunk_end)
                if metadata.get("complete") is not True:
                    raise MalformedResponseError("historical provider coverage is incomplete")
                for row in sorted(rows, key=lambda value: str(value.get("snapshotTimeUTC") or value.get("snapshotTime") or "")):
                    try:
                        candle = normalize_historical_price(row, instrument_id=instrument_id, epic=candidate.epic, timeframe=timeframe)
                    except (MalformedResponseError, InvalidOperation, TypeError, ValueError) as exc:
                        malformed_rows += 1
                        skipped_malformed_rows += 1
                        if len(malformed_diagnostics) < 100:
                            malformed_diagnostics.append(_malformed_row_diagnostic(row, metadata.get("metadata"), chunk_start, chunk_end, exc))
                        continue
                    if not (start <= candle.start < end):
                        continue
                    retrieved += 1
                    result = self.database.save_historical_candle(candle, provenance={"provider": "IG", "asset_source_type": "CFD", "market_name": market, "epic": candidate.epic, "provider_instrument_name": candidate.market_name, "currency": candidate.metadata.get("currency") or candidate.metadata.get("currencyCode"), "contract_metadata": candidate.metadata, "resolution": IG_RESOLUTIONS[timeframe], "retrieved_at": datetime.now(UTC).isoformat()})
                    inserted += result == "INSERTED"
                    skipped += result != "INSERTED"
                self.database.checkpoint_backfill_job(identity, chunk_end.isoformat(), retrieved, inserted, skipped, metadata, malformed_rows, skipped_malformed_rows, malformed_diagnostics)
                if self.client.pacing_seconds:
                    time.sleep(self.client.pacing_seconds)
            self.database.finish_backfill_job(identity, "COMPLETE", retrieved, inserted, skipped, malformed_rows, skipped_malformed_rows, malformed_diagnostics)
            return {"status": "COMPLETE", "job_id": identity, "market": market, "instrument_id": instrument_id, "epic": candidate.epic, "timeframe": timeframe, "retrieved": retrieved, "inserted": inserted, "skipped": skipped, "malformed_rows": malformed_rows, "skipped_malformed_rows": skipped_malformed_rows, "malformed_diagnostics": malformed_diagnostics, "requested_start": start.isoformat(), "requested_end": end.isoformat(), "available_range": self.database.available_history(instrument_id, timeframe)}
        except RateLimitError:
            self.database.finish_backfill_job(identity, "PAUSED", retrieved, inserted, skipped, malformed_rows, skipped_malformed_rows, malformed_diagnostics)
            return {"status": "BACKFILL_PAUSED_RATE_LIMIT", "job_id": identity, "retrieved": retrieved, "inserted": inserted, "skipped": skipped, "malformed_rows": malformed_rows, "skipped_malformed_rows": skipped_malformed_rows}
        except Exception:
            self.database.finish_backfill_job(identity, "FAILED", retrieved, inserted, skipped, malformed_rows, skipped_malformed_rows, malformed_diagnostics)
            raise


class ReplayService:
    def __init__(self, database, *, max_history: int = 600):
        self.database, self.max_history = database, max_history

    def run(self, *, instrument_id: str, start: datetime, end: datetime, window: str = "3Y") -> dict[str, Any]:
        job_id = self.database.deterministic_replay_job_id(instrument_id, start.isoformat(), end.isoformat(), window)
        prior = self.database.get_replay_job(job_id)
        self.database.start_replay_job(job_id, instrument_id, start.isoformat(), end.isoformat(), window)
        replay = HistoricalReplay(max_history=self.max_history)
        engine = ResearchEngine(self.database)
        phase2b = Phase2BEngine()
        processed = snapshots = outcomes = 0
        persisted_snapshots = int(prior.get("snapshots_generated", 0)) if prior else 0
        persisted_outcomes = int(prior.get("outcomes_written", 0)) if prior else 0
        checkpoint = parse_ig_timestamp(prior["last_information_time"]) if prior and prior.get("last_information_time") else None
        buffers = {timeframe: deque(maxlen=self.max_history + 100) for timeframe in SUPPORTED_TIMEFRAMES}
        iterators = {timeframe: iter(self.database.iter_candles(instrument_id, timeframe, end_at=(end + timedelta(days=1)).isoformat(), batch_size=250)) for timeframe in SUPPORTED_TIMEFRAMES}
        pending: dict[str, Candle | None] = {}
        for timeframe, iterator in iterators.items():
            pending[timeframe] = next(iterator, None)

        def advance(timeframe: str, cutoff: datetime) -> None:
            while pending[timeframe] is not None and pending[timeframe].end <= cutoff:
                buffers[timeframe].append(pending[timeframe])
                pending[timeframe] = next(iterators[timeframe], None)

        while pending["1H"] is not None:
            target = pending["1H"]
            buffers["1H"].append(target)
            pending["1H"] = next(iterators["1H"], None)
            # Model input is strictly point-in-time. Future outcome candles
            # are fetched separately only after the snapshot is persisted.
            advance("15M", target.end)
            advance("4H", target.end)
            advance("1D", target.end)
            if not (start <= target.start < end):
                continue
            if checkpoint and target.end <= checkpoint:
                continue
            aligned = {timeframe: list(buffer) for timeframe, buffer in buffers.items()}
            item = replay.state_for_aligned(target, aligned)
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
            outcome_candles = list(self.database.iter_candles(instrument_id, "15M", start_at=target.end.isoformat(), end_at=(target.end + timedelta(days=1, minutes=15)).isoformat(), batch_size=128))
            outcomes += engine.complete_snapshot(snapshot_id, reference_time=target.end, reference_price=float(target.close), direction=state.get("direction"), candles=outcome_candles, horizons=HORIZONS)
            processed += 1
            snapshots += 1
            self.database.checkpoint_replay_job(job_id, target.end.isoformat(), persisted_snapshots + snapshots, persisted_outcomes + outcomes)
        outcome_rows = self.database.connection.execute(
            """
            SELECT ro.horizon, COUNT(*)
            FROM research_outcomes ro
            JOIN research_snapshots rs ON rs.snapshot_id = ro.snapshot_id
            WHERE rs.instrument_id=? AND rs.timeframe='1H'
              AND rs.snapshot_timestamp >= ? AND rs.snapshot_timestamp < ?
              AND ro.status='COMPLETE'
            GROUP BY ro.horizon
            ORDER BY ro.horizon
            """,
            (instrument_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        outcome_counts = {horizon: count for horizon, count in outcome_rows}
        self.database.finish_replay_job(job_id, "COMPLETE", processed, persisted_snapshots + snapshots, persisted_outcomes + outcomes)
        return {
            "status": "COMPLETE",
            "job_id": job_id,
            "processed": processed,
            "snapshots": snapshots,
            "outcomes": outcomes,
            "candidate_snapshots": processed,
            "valid_snapshots": snapshots,
            "persisted_snapshots": persisted_snapshots + snapshots,
            "outcome_counts": outcome_counts,
        }
