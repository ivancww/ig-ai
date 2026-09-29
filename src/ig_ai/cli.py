from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import Database
from .discovery import discover_market_groups, select_stream_instruments
from .exceptions import IGHTTPError, MalformedResponseError
from .history import SUPPORTED_TIMEFRAMES, BackfillService, HistoricalIGClient, ReplayService
from .models import Instrument
from .phase1 import run_phase1_check
from .reporting import update_terminal_report, write_runtime_record
from .research import ResearchConfig, ResearchEngine
from .rest import IGRestClient
from .runtime import PersistedStream
from .security import SecretRedactionFilter
from .stream_smoke import format_smoke_result, run_stream_smoke
from .streaming import (
    IGStreamService,
    OfficialLightstreamerTransport,
    Subscription,
    lightstreamer_password,
)


def direction_evidence_sections(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """Return evidence relative to the primary model direction, not labels."""
    direction = snapshot.get("direction")
    supporting = [item for item in snapshot.get("evidence_ledger", []) if item.get("direction") == direction]
    opposing = [item for item in snapshot.get("evidence_ledger", []) if item.get("direction") in {"UP", "DOWN"} and item.get("direction") != direction]
    return supporting, opposing


def research_provenance_status(database, instruments: list[str]) -> str:
    """Report provenance only when persisted source identities are inspected."""
    connection = getattr(database, "connection", None)
    if connection is None or not instruments:
        return "NOT OBSERVED"
    placeholders = ",".join("?" for _ in instruments)
    rows = connection.execute(
        f"SELECT DISTINCT source_identity FROM research_snapshots WHERE instrument_id IN ({placeholders}) ORDER BY source_identity",
        instruments,
    ).fetchall()
    identities = [row[0] for row in rows if row[0]]
    required_parts = ("ig_cfd|", "instrument=", "epic=", "market=", "type=")
    if identities and all(all(part in identity for part in required_parts) for identity in identities):
        return "PASS: " + "; ".join(identities)
    return "NOT VERIFIED"


def main() -> int:
    parser = argparse.ArgumentParser(prog="ig-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-init")
    commands.add_parser("discover")
    commands.add_parser("rest-check")
    commands.add_parser("phase1-check")
    technical_parser = commands.add_parser("technical-status")
    technical_parser.add_argument("--instrument", default=None)
    technical_parser.add_argument("--timeframe", choices=("15M", "1H", "4H", "1D"), default="1H")
    pattern_parser = commands.add_parser("pattern-status")
    pattern_parser.add_argument("--instrument", default=None)
    pattern_parser.add_argument("--timeframe", choices=("15M", "1H", "4H", "1D"), default="1H")
    direction_parser = commands.add_parser("direction-status")
    direction_parser.add_argument("--instrument", default=None)
    direction_parser.add_argument("--timeframe", choices=("15M", "1H", "4H", "1D"), default="1H")
    alerts_parser = commands.add_parser("alerts")
    alerts_parser.add_argument("--instrument", default=None)
    alerts_parser.add_argument("--priority", choices=("INFO", "WATCH", "WARNING", "CRITICAL"), default=None)
    alerts_parser.add_argument("--limit", type=int, default=20)
    monitor_status_parser = commands.add_parser("monitor-status")
    monitor_status_parser.add_argument("--instrument", default=None)
    research_filters = {
        "--instrument": {"default": None}, "--window": {"choices": ("1Y", "3Y", "5Y"), "default": "3Y"},
        "--timeframe": {"choices": ("15M", "1H", "4H", "1D"), "default": "1H"}, "--direction": {"choices": ("UP", "DOWN"), "default": None},
        "--score-bucket": {"default": None}, "--trend-stage": {"default": None}, "--reversal-risk": {"default": None},
        "--agreement": {"default": None}, "--four-hour-alignment": {"default": None}, "--pattern": {"default": None}, "--alert-type": {"default": None}, "--horizon": {"default": None}, "--as-of": {"default": None},
    }
    research_parser = commands.add_parser("research", help="run an explicit local historical research job")
    for option, kwargs in research_filters.items():
        research_parser.add_argument(option, **kwargs)
    research_status_parser = commands.add_parser("research-status", help="inspect available historical research samples")
    for option, kwargs in research_filters.items():
        research_status_parser.add_argument(option, **kwargs)
    history_parser = commands.add_parser("history-backfill", help="read-only IG CFD historical price backfill")
    history_parser.add_argument("--market", choices=("US Tech 100", "Japan 225", "Hong Kong HS50"))
    history_parser.add_argument("--timeframe", choices=SUPPORTED_TIMEFRAMES, default="1H")
    history_parser.add_argument("--days", type=int)
    history_parser.add_argument("--start")
    history_parser.add_argument("--end")
    history_parser.add_argument("--resume", action="store_true")
    history_parser.add_argument("--all-markets", action="store_true")
    history_parser.add_argument("--dry-run", action="store_true")
    history_status_parser = commands.add_parser("history-status", help="inspect IG CFD historical coverage")
    history_status_parser.add_argument("--instrument", default=None)
    replay_parser = commands.add_parser("history-replay", help="rebuild point-in-time historical model states")
    replay_parser.add_argument("--market", default=None)
    replay_parser.add_argument("--start", required=True)
    replay_parser.add_argument("--end", required=True)
    replay_parser.add_argument("--window", choices=("1Y", "3Y", "5Y"), default="3Y")
    stream_parser = commands.add_parser("stream")
    stream_parser.add_argument("--duration", type=float, default=300.0)
    stream_parser.add_argument("--markets", default="US Tech 100,Japan 225,Hong Kong HS50")
    stream_parser.add_argument("--verbose", action="store_true")
    monitor_parser = commands.add_parser("monitor", help="run the read-only monitor for a wall-clock duration in seconds")
    monitor_parser.add_argument("--duration", type=float, default=3600.0)
    monitor_parser.add_argument("--markets", default="US Tech 100,Japan 225,Hong Kong HS50")
    monitor_parser.add_argument("--verbose", action="store_true")
    smoke_parser = commands.add_parser("stream-smoke")
    smoke_parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    runtime_stage = "command dispatch"
    historical_request_issued = False
    historical_client = None
    try:
        if args.command == "phase1-check":
            code, output = run_phase1_check(Path(__file__).resolve().parents[2])
            print(output)
            return code
        if args.command == "db-init":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            database.close()
            update_terminal_report(
                command="ig-ai db-init",
                account_type=settings.account_type,
                checks="database initialization passed",
                not_verified="IG authentication, market discovery, and streaming were not run",
            )
            print(f"Database initialized at {settings.database_path}")
            return 0
        if args.command == "technical-status":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                instruments = database.list_instruments()
                selected = args.instrument or (instruments[0][0] if instruments else None)
                if selected is None:
                    print("No instruments or technical features available.")
                    return 0
                records = database.get_technical_features(selected, args.timeframe)
                if not records:
                    print(f"No technical features for {selected} {args.timeframe}.")
                    return 0
                feature = records[0]
                ma = feature["moving_averages"]
                print(f"{selected} {args.timeframe}")
                for name in ("ema10", "ema20", "ema50", "ema100", "ema200"):
                    print(f"{name.upper()}: {ma[name]['value']}")
                print(f"RSI14: {feature['rsi14']['value']} ({feature['rsi14']['state']})")
                print(f"MACD: {feature['macd']['line']} / signal {feature['macd']['signal']} / histogram {feature['macd']['histogram']}")
                print(f"ATR14: {feature['atr14']['value']}")
                print(f"BB Position: {feature['bollinger']['zone']}")
                print(f"Structure: {feature['structure']}")
                print(f"Candle state: {feature['candle_state']}")
            finally:
                database.close()
            return 0
        if args.command == "pattern-status":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                instruments = database.list_instruments()
                selected = args.instrument or (instruments[0][0] if instruments else None)
                if selected is None:
                    print("No instruments or Phase 2B observations available.")
                    return 0
                status = database.get_phase2b_status(selected, args.timeframe)
                print(f"{selected} {args.timeframe}")
                print(f"Structure: {status['structure']}")
                print("Patterns:")
                for pattern in status["patterns"]:
                    print(f"  {pattern['pattern']}: {pattern.get('lifecycle')} ({pattern.get('candle_state')})")
                print(f"Direction/reversal evidence: {status['direction_reversal']}")
                print("Inspection only; no trading recommendation.")
            finally:
                database.close()
            return 0
        if args.command == "direction-status":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                instruments = database.list_instruments()
                selected = args.instrument or (instruments[0][0] if instruments else None)
                if selected is None:
                    print("No instruments or direction snapshots available.")
                    return 0
                if args.timeframe != "1H":
                    print("Direction model reference timeframe is 1H; no independent direction model exists for this timeframe.")
                    return 0
                snapshot = database.get_direction_status(selected, args.timeframe)
                if snapshot is None:
                    print(f"No direction snapshot for {selected} {args.timeframe}.")
                    return 0
                print(selected)
                print(f"Primary Direction: {snapshot['direction']}")
                reference = snapshot.get("model_reference", snapshot)
                print(f"Model Reference: {reference['timeframe']} {reference['candle_timestamp']}")
                print(f"UP Score: {snapshot['up_score']}")
                print(f"DOWN Score: {snapshot['down_score']}")
                print(f"Trend Stage: {snapshot['trend_stage']}")
                print(f"Holding Window: {snapshot['holding_window']}")
                print(f"Reversal Risk: {snapshot['reversal_risk']['category']}")
                print(f"Timeframe Agreement: {snapshot['timeframe_agreement']['status']}")
                supporting, opposing = direction_evidence_sections(snapshot)
                print("Supporting Evidence:")
                for item in supporting:
                    print(f"  - {item['timeframe']} {item['evidence_type']}: {item['raw_state']}")
                print("Opposing/Risk Evidence:")
                for item in opposing:
                    print(f"  - {item['timeframe']} {item['evidence_type']}: {item['raw_state']}")
                print("Technical score only — not a calibrated probability or trading recommendation.")
            finally:
                database.close()
            return 0
        if args.command == "alerts":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                for alert in database.list_alerts(args.instrument, args.priority, max(1, args.limit)):
                    print(f"[{alert['priority']}] {alert['instrument']} {alert['alert_type']} {alert['created_at']}")
                    print(alert["message"])
                    print()
            finally:
                database.close()
            return 0
        if args.command == "monitor-status":
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                status = database.get_monitor_status(args.instrument)
                print(f"Accepted monitor states: {len(status['states'])}")
                print(f"Alert count: {status['alert_count']}")
                for state in status["states"]:
                    print(f"{state.get('instrument')}: {state.get('model_reference')} direction={state.get('direction')} stage={state.get('trend_stage')} risk={(state.get('reversal_risk') or {}).get('category')}")
                if status["last_alert"]:
                    print(f"Last alert: {status['last_alert']['alert_type']} ({status['last_alert']['priority']}) at {status['last_alert']['created_at']}")
                print("Process liveness is not reported unless an external monitor provides it.")
            finally:
                database.close()
            return 0
        if args.command in {"research", "research-status"}:
            runtime_stage = "local research setup"
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                engine = ResearchEngine(database, ResearchConfig(default_window=args.window))
                instruments = [args.instrument] if args.instrument else [row[0] for row in database.list_instruments()]
                as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
                research_results = []
                if args.command == "research" and not args.pattern and not args.alert_type:
                    runtime_stage = "local research execution"
                    for instrument in instruments:
                        result = engine.run_direction_research(instrument_id=instrument, window=args.window, now=as_of)
                        research_results.append((instrument, result))
                        available = result["available_history"]
                        print(f"{instrument}: recorded={result['recorded_snapshots']} outcomes={result['outcomes_written']} requested={args.window} available_days={available['days']}")
                        for timeframe in ("15M", "1H", "4H", "1D"):
                            pattern_result = engine.run_pattern_research(instrument_id=instrument, timeframe=timeframe, window=args.window, now=as_of)
                            print(f"{instrument} {timeframe} patterns/divergences: recorded={pattern_result['recorded_snapshots']} outcomes={pattern_result['outcomes_written']}")
                        alert_result = engine.run_alert_research(instrument_id=instrument, window=args.window, now=as_of)
                        print(f"{instrument} alerts: recorded={alert_result['recorded_snapshots']} outcomes={alert_result['outcomes_written']}")
                if args.command == "research-status":
                    for instrument in instruments:
                        available = database.available_history(instrument, args.timeframe, requested_window=args.window)
                        print(f"{instrument} {args.timeframe}: requested={args.window} available_days={available['days']} status={available['status']}")
                summary = engine.summary(window=args.window, as_of=as_of, instrument_id=args.instrument, timeframe=args.timeframe, direction=args.direction, score_bucket=args.score_bucket, trend_stage=args.trend_stage, reversal_risk=args.reversal_risk, agreement=args.agreement, four_hour_alignment=args.four_hour_alignment, pattern_name=args.pattern, alert_type=args.alert_type, horizon=args.horizon)
                print(f"Research Engine: {summary['research_version']}")
                print(f"As of: {summary['as_of']}")
                print(f"Samples: {summary['sample_count']} completed snapshots; eligible snapshots: {summary['snapshot_count']}; outcome observations: {summary['outcome_observation_count']}")
                print(f"Status: {summary['quality']}")
                for row in summary["rows"]:
                    count = row["sample_count"]
                    print(f"Future {row['horizon']}: n={count} UP={row['up_percentage']}% DOWN={row['down_percentage']}% FLAT={row['flat_percentage']}% average_move={row['average_move']} median_mfe={row['median_mfe']} median_mae={row['median_mae']}")
                comparison = database.research_4h_comparison(instrument_id=args.instrument, timeframe=args.timeframe, horizon=args.horizon)
                print(f"4H value study: {comparison}")
                if not summary["rows"]:
                    print("No completed historical outcomes are available; pending outcomes are not counted as samples.")
                update_terminal_report(
                    command=f"ig-ai {args.command}",
                    account_type=settings.account_type,
                    authentication="NOT RUN (local SQLite workflow)",
                    runtime_outcome="COMPLETED",
                    final_state=f"{args.command.upper()} PASS",
                    checks="local historical research/reporting completed",
                    not_verified="No IG provider request was issued by this command",
                    details={
                        "stage reached": "local research summary",
                        "market": args.instrument or "ALL PERSISTED INSTRUMENTS",
                        "timeframe": args.timeframe,
                        "replay target/range": f"window={args.window}; as_of={summary['as_of']}",
                        "candidate snapshots": summary["snapshot_count"],
                        "valid snapshots": summary["sample_count"],
                        "persisted snapshots": ", ".join(
                            f"{instrument}={result['recorded_snapshots']}" for instrument, result in research_results
                        ) or "STATUS ONLY",
                        "completed outcome counts by horizon": ", ".join(
                            f"{row['horizon']}={row['sample_count']}" for row in summary["rows"]
                        ) or "NONE",
                        "anti-lookahead": "PASS (closed-candle research engine)",
                        "replay": "NOT RUN (research command does not execute Historical Replay)",
                        "outcome": "PASS",
                        "provenance": research_provenance_status(database, instruments),
                        "final state": summary["quality"],
                    },
                )
            finally:
                database.close()
            return 0
        if args.command == "history-status":
            runtime_stage = "historical status setup"
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                runtime_stage = "historical status inspection"
                rows = database.history_status(args.instrument)
                if not rows:
                    print("No historical candles available. Source label: IG CFD.")
                for row in rows:
                    first = datetime.fromisoformat(row["earliest"])
                    last = datetime.fromisoformat(row["latest"])
                    print(f"{row['market_name'] or row['instrument_id']} {row['timeframe']}: source=IG CFD ({row['source']}) EPIC={row['epic']} earliest={row['earliest']} latest={row['latest']} closed={row['closed_count']} available_days={(last-first).total_seconds()/86400:.2f} backfill_status={row['backfill_status']} last_successful_retrieval={row['last_successful_retrieval'] or 'NONE'} requested={row['requested_start'] or 'NONE'}..{row['requested_end'] or 'NONE'} retrieved_through={row['retrieved_through'] or 'NONE'} largest_gap_seconds={row['largest_gap'] if row['largest_gap'] is not None else 'NONE'} session_gap_uncertainty={str(row['session_gap_uncertainty']).upper()}")
                update_terminal_report(
                    command="ig-ai history-status",
                    account_type=settings.account_type,
                    authentication="NOT RUN (local SQLite workflow)",
                    runtime_outcome="COMPLETED",
                    final_state="HISTORY STATUS PASS",
                    checks="local historical coverage inspection completed",
                    not_verified="No IG provider request was issued by this command",
                    details={
                        "stage reached": "historical status inspection",
                        "market": args.instrument or "ALL PERSISTED INSTRUMENTS",
                        "records": len(rows),
                        "persisted closed candles": "; ".join(
                            f"{row['market_name'] or row['instrument_id']} {row['timeframe']}={row['closed_count']}"
                            for row in rows
                        ) or "NONE",
                        "provenance": "; ".join(
                            f"{row['market_name'] or row['instrument_id']} {row['timeframe']}={row['source']}"
                            for row in rows
                        ) or "NOT OBSERVED",
                        "final state": "PASS",
                    },
                )
                return 0
            finally:
                database.close()
        if args.command == "history-replay":
            runtime_stage = "historical replay setup"
            settings = Settings.from_env(require_credentials=False)
            database = Database(settings.database_path)
            try:
                runtime_stage = "local historical replay execution"
                instruments = database.list_instruments()
                selected = [args.market] if args.market else [row[0] for row in instruments]
                start = datetime.fromisoformat(args.start).astimezone(UTC)
                end = datetime.fromisoformat(args.end).astimezone(UTC)
                replay_results = []
                for instrument in selected:
                    instrument_id = instrument if instrument.startswith("ig:") else next((row[0] for row in instruments if row[2] == instrument), instrument)
                    result = ReplayService(database).run(instrument_id=instrument_id, start=start, end=end, window=args.window)
                    replay_results.append((instrument, result))
                    print(f"{instrument_id}: status={result['status']} snapshots={result['snapshots']} outcomes={result['outcomes']}")
                update_terminal_report(
                    command="ig-ai history-replay",
                    account_type=settings.account_type,
                    authentication="NOT RUN (local SQLite workflow)",
                    runtime_outcome="COMPLETED",
                    final_state="HISTORICAL REPLAY PASS",
                    checks="local point-in-time historical replay completed",
                    not_verified="No IG provider request was issued by this command",
                    details={
                        "stage reached": "local historical replay execution",
                        "market": args.market or "ALL PERSISTED INSTRUMENTS",
                        "timeframe": "1H model reference",
                        "replay target/range": f"{args.start} to {args.end}; window={args.window}",
                        "candidate snapshots": ", ".join(
                            f"{instrument}={result.get('candidate_snapshots', result['processed'])}" for instrument, result in replay_results
                        ) or "NONE",
                        "valid snapshots": ", ".join(
                            f"{instrument}={result.get('valid_snapshots', result['snapshots'])}" for instrument, result in replay_results
                        ) or "NONE",
                        "persisted snapshots": ", ".join(
                            f"{instrument}={result.get('persisted_snapshots', result['snapshots'])}" for instrument, result in replay_results
                        ) or "NONE",
                        "completed outcome counts by horizon": ", ".join(
                            f"{instrument}=" + (
                                ",".join(f"{horizon}={count}" for horizon, count in result.get("outcome_counts", {}).items())
                                or f"total={result['outcomes']}"
                            )
                            for instrument, result in replay_results
                        ) or "NONE",
                        "anti-lookahead": "NOT INDEPENDENTLY RE-VERIFIED (implementation invariant)",
                        "replay": "PASS",
                        "outcome": "PASS",
                        "provenance": research_provenance_status(
                            database,
                            [
                                instrument if instrument.startswith("ig:") else next(
                                    (row[0] for row in instruments if row[2] == instrument), instrument
                                )
                                for instrument, _result in replay_results
                            ],
                        ),
                        "final state": "PASS",
                    },
                )
                return 0
            finally:
                database.close()
        if args.command == "history-backfill":
            runtime_stage = "historical backfill argument validation"
            if args.all_markets and args.market:
                raise ValueError("--all-markets and --market are mutually exclusive")
            if sum(value is not None for value in (args.days, args.start)) > 1 or (args.end and not args.start) or (args.start and not args.end):
                raise ValueError("use exactly one of --days or --start/--end")
            if args.days is not None and args.days <= 0:
                raise ValueError("--days must be positive")
            markets = ["US Tech 100", "Japan 225", "Hong Kong HS50"] if args.all_markets else ([args.market] if args.market else [])
            if not markets:
                raise ValueError("--market or --all-markets is required")
            runtime_stage = "historical configuration/authentication"
            settings = Settings.from_env()
            database = Database(settings.database_path)
            try:
                end = datetime.now(UTC) if not args.end else datetime.fromisoformat(args.end).astimezone(UTC)
                start = end - timedelta(days=args.days) if args.days is not None else datetime.fromisoformat(args.start).astimezone(UTC)
                runtime_stage = "historical authentication/discovery"
                historical_client = HistoricalIGClient(IGRestClient(settings), pacing_seconds=float(os.environ.get("IG_HISTORY_PACING_SECONDS", "1")))
                service = BackfillService(database, historical_client)
                results = []
                for market in markets:
                    runtime_stage = f"historical backfill for {market}"
                    result = service.run(market=market, timeframe=args.timeframe, start=start, end=end, dry_run=args.dry_run, resume=args.resume)
                    historical_request_issued = historical_request_issued or historical_client.request_count > 0
                    results.append(result)
                    print(json.dumps(result, sort_keys=True, default=str))
                historical_request_issued = historical_request_issued or historical_client.request_count > 0
                successful = all(result.get("status") in {"COMPLETE", "DRY_RUN"} for result in results)
                dry_run = bool(results) and all(result.get("status") == "DRY_RUN" for result in results)
                update_terminal_report(
                    command="ig-ai history-backfill",
                    account_type=settings.account_type,
                    authentication="NOT RUN (dry run)" if dry_run else "PASS (read-only historical workflow)",
                    runtime_outcome="PLANNED" if dry_run else "COMPLETED",
                    final_state="HISTORICAL BACKFILL DRY RUN" if dry_run else ("HISTORICAL BACKFILL PASS" if successful else "HISTORICAL BACKFILL FAIL"),
                    checks="dry-run request planning completed; no historical request issued" if dry_run else "read-only IG historical backfill completed",
                    not_verified="Authentication/provider access and historical request were not verified; trading/order/position endpoints were not used" if dry_run else "Trading/order/position endpoints were not used",
                    details={
                        "stage reached": "historical persistence",
                        "market": "; ".join(result.get("market", market) for result, market in zip(results, markets, strict=True)),
                        "EPIC": "; ".join(result.get("epic", "NOT RESOLVED") for result in results),
                        "timeframe": args.timeframe,
                        "requested range": "; ".join(
                            f"{result.get('requested_start', start.isoformat())} to {result.get('requested_end', end.isoformat())}"
                            for result in results
                        ),
                        "provider range": "; ".join(
                            f"{result.get('available_range', {}).get('first', 'NOT AVAILABLE')} to {result.get('available_range', {}).get('last', 'NOT AVAILABLE')}"
                            for result in results
                        ),
                        "retrieved": "; ".join(str(result.get("retrieved", 0)) for result in results),
                        "inserted": "; ".join(str(result.get("inserted", 0)) for result in results),
                        "skipped": "; ".join(str(result.get("skipped", 0)) for result in results),
                        "malformed rows": "; ".join(str(result.get("malformed_rows", 0)) for result in results),
                        "skipped malformed rows": "; ".join(str(result.get("skipped_malformed_rows", 0)) for result in results),
                        "malformed gap semantics": "PROVIDER_MALFORMED_ROW_GAP when malformed rows are reported; otherwise NONE",
                        "persisted closed candle count": "; ".join(
                            str(result.get("available_range", {}).get("candle_count", 0)) for result in results
                        ),
                        "earliest/latest": "; ".join(
                            f"{result.get('available_range', {}).get('first', 'NONE')} / {result.get('available_range', {}).get('last', 'NONE')}"
                            for result in results
                        ),
                        "provenance": "NOT APPLICABLE (dry run)" if dry_run else "IG_HISTORICAL / IG / CFD",
                        "historical request issued": "NO" if dry_run else ("YES" if historical_request_issued else "NO"),
                        "final state": "DRY_RUN" if dry_run else ("PASS" if successful else "FAIL"),
                    },
                )
                return 0
            finally:
                database.close()
        client = IGRestClient(Settings.from_env())
        if args.command == "rest-check":
            client.authenticate()
            update_terminal_report(
                command="ig-ai rest-check",
                account_type=client.settings.account_type,
                authentication="PASS",
                checks="read-only authentication check passed",
                not_verified="Market discovery and streaming were not run",
                secrets=(client.settings.api_key, client.settings.username, client.settings.password),
            )
            print("IG authentication succeeded (read-only verification)")
        elif args.command == "discover":
            groups = discover_market_groups(client)
            safe_results = []
            for group in groups:
                print(f"{group.requested_market}: {group.status}")
                safe_results.append(f"{group.requested_market}: {group.status}")
                detail_line = f"  detail calls: {group.detail_calls}/{group.detail_budget}"
                safe_results.append(detail_line)
                print(detail_line)
                report_candidates = [
                    candidate
                    for candidate in group.candidates
                    if candidate.verified
                    or candidate.instrument_type in {"INDICES", "INDEX"}
                ]
                for candidate in report_candidates:
                    line = (
                        f"  {candidate.market_name}\t{candidate.epic}\t"
                        f"{candidate.market_status or ''}\t{candidate.instrument_type or ''}\t"
                        f"{candidate.expiry or ''}\t{candidate.classification}\t"
                        f"eligible_primary={candidate.eligible_primary}\tverified={candidate.verified}"
                    )
                    if candidate.exclusion_reason:
                        line += f"\texcluded={candidate.exclusion_reason}"
                    safe_results.append(line)
                    print(line)
                if group.ambiguity_reason:
                    ambiguity_line = f"  ambiguity: {group.ambiguity_reason}"
                    safe_results.append(ambiguity_line)
                    print(ambiguity_line)
                if group.requested_market == "Hong Kong HS50":
                    variant_line = (
                        f"  eligible weekday cash variants: {len(group.verified_variants)}"
                    )
                    safe_results.append(variant_line)
                    print(variant_line)
            update_terminal_report(
                command="ig-ai discover",
                account_type=client.settings.account_type,
                authentication="PASS (discovery request completed)",
                market_discovery=(
                    "PASS — " + ", ".join(f"{group.requested_market}: {group.status}" for group in groups)
                ),
                checks="discovery runtime check passed",
                not_verified="Streaming and trade execution were not run; candidates are not trading instructions",
                output="\n".join(safe_results),
                secrets=(client.settings.api_key, client.settings.username, client.settings.password),
            )
        elif args.command in {"stream", "monitor"}:
            if args.duration <= 0:
                raise ValueError("--duration must be positive")
            groups = discover_market_groups(client)
            groups_by_market = {group.requested_market: group for group in groups}
            requested = {name.strip() for name in args.markets.split(",") if name.strip()}
            selected = [candidate for candidate in select_stream_instruments(groups) if candidate.requested_market in requested]
            missing = sorted(requested - {candidate.requested_market for candidate in selected})
            if missing:
                raise RuntimeError("No provider-verified weekday cash instrument for: " + ", ".join(missing))
            session = client.ensure_session()
            endpoint = session.lightstreamer_endpoint
            if not endpoint:
                raise RuntimeError("IG authentication did not provide a Lightstreamer endpoint")
            account_id = session.account_id
            if not account_id:
                raise RuntimeError("IG authentication did not provide the active account identifier")
            database = Database(client.settings.database_path)
            instruments = {}
            for candidate in selected:
                instrument_id = candidate.epic
                instrument = Instrument(
                    instrument_id, candidate.epic, candidate.market_name,
                    candidate.instrument_type, candidate.market_status, candidate.metadata,
                )
                instruments[instrument_id] = instrument
                database.save_instrument_model(instrument)
            stream = IGStreamService(
                endpoint,
                account_id,
                lightstreamer_password(session.cst, session.security_token),
                OfficialLightstreamerTransport(),
                reconnect_seconds=client.settings.stream_reconnect_seconds,
            )
            for instrument in instruments.values():
                stream.add_subscription(
                    Subscription(
                        instrument.instrument_id,
                        f"PRICE:{account_id}:{instrument.epic}",
                    )
                )
            sink = PersistedStream(database, instruments, client.settings.market_timezone)
            write_runtime_record({
                "status": "RUNNING",
                "requested_duration": args.duration,
                "started_monotonic": time.monotonic(),
            })
            try:
                sink.run_for(stream, args.duration)
            finally:
                database.close()
            audit = sink.runtime_audit
            write_runtime_record({"status": "COMPLETED", **audit})
            instrument_ids = [instrument.instrument_id for instrument in instruments.values()]
            validation_passed = stream.stats.live_validation_passed(instrument_ids)
            reconnect_outcome = stream.reconnect_status
            validation_lines = []
            for candidate in selected:
                group = groups_by_market[candidate.requested_market]
                subscription_id = str(selected.index(candidate) + 1)
                subscription_ok = subscription_id in stream.stats.diagnostics.subscriptions_accepted
                update_ok = stream.stats.updates_received.get(candidate.epic, 0) > 0
                line = (
                    f"{candidate.requested_market}: discovery: {group.status}; "
                    f"subscription: {'YES' if subscription_ok else 'NO'}; "
                    f"ItemUpdate: {'YES' if update_ok else 'NO'}"
                )
                if candidate.requested_market == "Hong Kong HS50":
                    line += (
                        f"; eligible weekday cash variants: {len(group.verified_variants)}"
                        f"; selected weekday cash variant: {candidate.market_name} ({candidate.epic})"
                    )
                line += (
                    f"; received={sink.received_item_updates.get(candidate.epic, 0)}"
                    f"; normalized={sink.normalized_observations.get(candidate.epic, 0)}"
                    f"; persisted={sink.observations_persisted.get(candidate.epic, 0)}"
                    f"; persistence_failures={sink.persistence_failures.get(candidate.epic, 0)}"
                    + "; " + ", ".join(
                        f"{timeframe}: forming={sink.forming_candles_by_instrument[candidate.epic][timeframe]}"
                        f", finalized={sink.finalized_candles_by_instrument[candidate.epic][timeframe]}"
                        f", upserts={sink.candle_upserts_by_instrument[candidate.epic][timeframe]}"
                        for timeframe in sink.timeframes
                    )
                )
                if not update_ok:
                    if sink.runtime_failures:
                        line += "; no ItemUpdate classification: processing failure recorded"
                    else:
                        line += "; no ItemUpdate during window; runtime healthy/provider inactivity possible"
                validation_lines.append(line)
            runtime_failures = "; ".join(
                "Failure stage={failure_stage}; Exception type={exception_type}; "
                "Affected market/instrument={affected_market}".format(**failure)
                for failure in sink.runtime_failures
            )
            warning_lines = [*stream.stats.warnings]
            if runtime_failures:
                warning_lines.append(runtime_failures)
            persistence_passed = sum(sink.persistence_failures.values()) == 0 and sum(sink.observations_persisted.values()) > 0
            candle_pipeline_passed = all(
                sink.candle_upserts_by_instrument[instrument.instrument_id][timeframe] > 0
                for instrument in instruments.values()
                for timeframe in sink.timeframes
            )
            duration_complete = audit["exit_reason"] == "DURATION_COMPLETE"
            elapsed_tolerance = max(2.0, args.duration * 0.05)
            long_run_passed = (
                duration_complete
                and abs(audit["actual_elapsed"] - args.duration) <= elapsed_tolerance
                and not sum(sink.persistence_failures.values())
            )
            update_terminal_report(
                command=f"ig-ai stream --duration {args.duration:g}",
                account_type=client.settings.account_type,
                authentication="PASS",
                market_discovery="PASS — provider-verified weekday cash instruments selected",
                streaming=(
                    "Streaming client: OFFICIAL LIGHTSTREAMER SDK; "
                    f"Connection: {'VERIFIED' if stream.stats.diagnostics.connection_verified else 'NOT VERIFIED'}; "
                    f"{stream.stats.connection_state}; "
                    + "; ".join(validation_lines)
                    + "; "
                    + "; ".join(
                        f"{instrument.market_name}: updates={stream.stats.updates_received.get(instrument.instrument_id, 0)}, "
                        f"last_update={sink.last_update.get(instrument.instrument_id, 'NO LIVE TICKS YET')}"
                        for instrument in instruments.values()
                    )
                    + f"; subscriptions={len(stream.stats.diagnostics.subscriptions_accepted)}/{len(instruments)}; "
                    + f"markets receiving updates={sum(bool(value) for value in stream.stats.updates_received.values())}/{len(instruments)}; "
                    + f"observations normalized={sum(sink.normalized_observations.values())}; "
                    + f"observations persisted={sum(sink.observations_persisted.values())}; "
                    + f"persistence failures={sum(sink.persistence_failures.values())}; "
                    + ", ".join(
                        f"{timeframe}: forming={sum(sink.forming_candles_by_instrument[i.instrument_id][timeframe] for i in instruments.values())}"
                        f", finalized={sum(sink.finalized_candles_by_instrument[i.instrument_id][timeframe] for i in instruments.values())}"
                        f", upserts={sink.candle_upserts[timeframe]}"
                        for timeframe in sink.timeframes
                    ) + "; "
                    + f"requested duration={args.duration:g}s; actual elapsed={audit['actual_elapsed']:.3f}s; exit reason={audit['exit_reason']}; "
                    + f"reconnect/recovery={reconnect_outcome}; reconnects={stream.stats.reconnect_count}; "
                    + f"; SDK statuses={','.join(stream.stats.diagnostics.sdk_statuses) or 'NONE'}; "
                    + f"SDK client created={str(stream.stats.diagnostics.sdk_client_created).lower()}; "
                    + f"connect invoked={str(stream.stats.diagnostics.connect_invoked).lower()}; "
                    + f"connection wait={stream.stats.diagnostics.connection_wait_seconds:.3f}s; "
                    + f"client lifecycle={stream.stats.diagnostics.client_lifecycle_state}; "
                    + f"server error code={stream.stats.diagnostics.server_error_code or 'NONE'}; "
                    + f"server error message={stream.stats.diagnostics.server_error_message or 'NONE'}"
                ),
                runtime_outcome="COMPLETED",
                connection_established=(
                    "VERIFIED" if stream.stats.diagnostics.connection_verified else "NOT VERIFIED"
                ),
                session_established=(
                    "VERIFIED" if stream.stats.diagnostics.connection_verified else "NOT VERIFIED"
                ),
                subscriptions_accepted=(
                    f"VERIFIED ({len(stream.stats.diagnostics.subscriptions_accepted)}/{len(instruments)})"
                    if len(stream.stats.diagnostics.subscriptions_accepted) == len(instruments)
                    else f"NOT VERIFIED ({len(stream.stats.diagnostics.subscriptions_accepted)}/{len(instruments)})"
                ),
                real_price_updates=(
                    "VERIFIED (ALL MARKETS)" if validation_passed
                    else f"NOT VERIFIED ({sum(bool(value) for value in stream.stats.updates_received.values())}/{len(instruments)} markets)"
                ),
                final_state=(
                    "LIVE MULTI-MARKET VALIDATION PASS"
                    if validation_passed
                    else "LIVE MULTI-MARKET VALIDATION FAIL"
                ),
                checks=(
                    "read-only Lightstreamer runtime completed; observations entered persistence/candle pipeline; "
                    f"STREAMING {'PASS' if validation_passed else 'FAIL'}; "
                    f"PERSISTENCE {'PASS' if persistence_passed else 'FAIL'}; "
                    f"CANDLE PIPELINE {'PASS' if candle_pipeline_passed else 'NOT VERIFIED'}; "
                    f"LONG-RUN {'PASS' if long_run_passed else 'FAIL'}; RECONNECT {reconnect_outcome}"
                ),
                warnings="; ".join(warning_lines) if warning_lines else "NONE",
                not_verified=(
                    "Candle validation is not claimed; this command validates live observations and pipeline capability"
                ),
                output="\n".join(validation_lines) + "\n" + "; ".join(
                    f"{candidate.market_name}\t{candidate.epic}" for candidate in selected
                ),
                secrets=(client.settings.api_key, client.settings.username, client.settings.password, session.cst, session.security_token),
            )
            for line in validation_lines:
                print(line)
            print(f"Requested duration: {args.duration:g}s")
            print(f"Actual elapsed: {audit['actual_elapsed']:.3f}s")
            print(f"Exit reason: {audit['exit_reason']}")
            print(f"STREAMING: {'PASS' if validation_passed else 'FAIL'}")
            print(f"PERSISTENCE: {'PASS' if persistence_passed else 'FAIL'}")
            print(f"CANDLE PIPELINE: {'PASS' if candle_pipeline_passed else 'NOT VERIFIED'}")
            print(f"LONG-RUN: {'PASS' if long_run_passed else 'FAIL'}")
            print(f"LIVE MULTI-MARKET STREAM VALIDATION: {'PASS' if validation_passed else 'FAIL'}")
            return 0 if validation_passed and long_run_passed else 1
        elif args.command == "stream-smoke":
            result = run_stream_smoke(client, args.duration)
            print(format_smoke_result(result))
            smoke_settings = getattr(client, "settings", None)
            update_terminal_report(
                command=f"ig-ai stream-smoke --duration {args.duration:g}",
                account_type=getattr(smoke_settings, "account_type", os.environ.get("IG_ACCOUNT_TYPE", "DEMO")),
                authentication="PASS" if result.authentication else "FAIL or not verified",
                market_discovery="PASS" if result.subscription_requested else "FAIL or not run",
                streaming="PASS" if result.passed else "FAIL",
                runtime_outcome="COMPLETED",
                connection_established="VERIFIED" if result.connect_invoked else "NOT VERIFIED",
                session_established="VERIFIED" if result.endpoint_received else "NOT VERIFIED",
                subscriptions_accepted="VERIFIED" if result.subscription_established else "NOT VERIFIED",
                real_price_updates="VERIFIED" if result.item_update_received else "NOT VERIFIED",
                final_state="STREAM SMOKE PASS" if result.passed else "STREAM SMOKE FAIL",
                checks="read-only Lightstreamer smoke test completed",
                warnings=result.error or "NONE",
                not_verified="No trade endpoints were used",
                details={
                    "stage reached": result.failure_stage or "completed",
                    "safe failure category": result.error_type or "NONE",
                    "final state": "PASS" if result.passed else "FAIL",
                },
                secrets=tuple(
                    getattr(smoke_settings, name, "")
                    for name in ("api_key", "username", "password")
                ),
            )
            return 0 if result.passed else 1
    except Exception as exc:
        safe_output = exc.safe_diagnostic() if isinstance(exc, (IGHTTPError, MalformedResponseError)) else type(exc).__name__
        historical_request_issued = historical_request_issued or bool(
            getattr(historical_client, "request_count", 0)
        )
        update_terminal_report(
            command=f"ig-ai {args.command}",
            account_type=os.environ.get("IG_ACCOUNT_TYPE", "DEMO"),
            authentication="FAIL or not verified",
            market_discovery="FAIL or not run",
            checks="command failed",
            warnings="Runtime error",
            not_verified="Successful completion was not verified",
            output=safe_output,
            details={
                "stage reached": runtime_stage,
                "historical request issued": "YES" if historical_request_issued else "NO",
                "safe failure category": safe_output,
                "final state": "FAIL",
            },
            secrets=(
                os.environ.get("IG_API_KEY", ""),
                os.environ.get("IG_USERNAME", ""),
                os.environ.get("IG_PASSWORD", ""),
            ),
        )
        raise
    return 0
