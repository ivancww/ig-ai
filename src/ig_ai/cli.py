from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from .config import Settings
from .database import Database
from .discovery import discover_market_groups, select_stream_instruments
from .exceptions import IGHTTPError
from .models import Instrument
from .phase1 import run_phase1_check
from .reporting import update_terminal_report, write_runtime_record
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
    stream_parser = commands.add_parser("stream")
    stream_parser.add_argument("--duration", type=float, default=300.0)
    stream_parser.add_argument("--markets", default="US Tech 100,Japan 225,Hong Kong HS50")
    stream_parser.add_argument("--verbose", action="store_true")
    monitor_parser = commands.add_parser("monitor")
    monitor_parser.add_argument("--duration", type=float, default=3600.0)
    monitor_parser.add_argument("--markets", default="US Tech 100,Japan 225,Hong Kong HS50")
    monitor_parser.add_argument("--verbose", action="store_true")
    smoke_parser = commands.add_parser("stream-smoke")
    smoke_parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
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
            return 0 if result.passed else 1
    except Exception as exc:
        safe_output = exc.safe_diagnostic() if isinstance(exc, IGHTTPError) else type(exc).__name__
        update_terminal_report(
            command=f"ig-ai {args.command}",
            account_type=os.environ.get("IG_ACCOUNT_TYPE", "DEMO"),
            authentication="FAIL or not verified",
            market_discovery="FAIL or not run",
            checks="command failed",
            warnings="Runtime error",
            not_verified="Successful completion was not verified",
            output=safe_output,
            secrets=(
                os.environ.get("IG_API_KEY", ""),
                os.environ.get("IG_USERNAME", ""),
                os.environ.get("IG_PASSWORD", ""),
            ),
        )
        raise
    return 0
