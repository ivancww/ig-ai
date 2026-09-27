from __future__ import annotations

import argparse
import logging
import os

from .config import Settings
from .database import Database
from .discovery import discover_market_groups, select_stream_instruments
from .exceptions import IGHTTPError
from .models import Instrument
from .reporting import update_terminal_report
from .rest import IGRestClient
from .runtime import PersistedStream
from .security import SecretRedactionFilter
from .streaming import (
    IGStreamService,
    Subscription,
    WebSocketLightstreamerTransport,
    lightstreamer_password,
    lightstreamer_ws_endpoint,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="ig-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-init")
    commands.add_parser("discover")
    commands.add_parser("rest-check")
    stream_parser = commands.add_parser("stream")
    stream_parser.add_argument("--duration", type=float, default=300.0)
    stream_parser.add_argument("--markets", default="US Tech 100,Japan 225,Hong Kong HS50")
    stream_parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
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
        elif args.command == "stream":
            if args.duration <= 0:
                raise ValueError("--duration must be positive")
            groups = discover_market_groups(client)
            requested = {name.strip() for name in args.markets.split(",") if name.strip()}
            selected = [candidate for candidate in select_stream_instruments(groups) if candidate.requested_market in requested]
            missing = sorted(requested - {candidate.requested_market for candidate in selected})
            if missing:
                raise RuntimeError("No provider-verified weekday cash instrument for: " + ", ".join(missing))
            session = client.ensure_session()
            endpoint = session.lightstreamer_endpoint
            if not endpoint:
                raise RuntimeError("IG authentication did not provide a Lightstreamer endpoint")
            endpoint = lightstreamer_ws_endpoint(endpoint)
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
                WebSocketLightstreamerTransport(),
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
            try:
                sink.run_for(stream, args.duration)
            finally:
                database.close()
            update_terminal_report(
                command=f"ig-ai stream --duration {args.duration:g}",
                account_type=client.settings.account_type,
                authentication="PASS",
                market_discovery="PASS — provider-verified weekday cash instruments selected",
                streaming=(
                    f"{stream.stats.connection_state}; "
                    + "; ".join(
                        f"{instrument.market_name}: updates={stream.stats.updates_received.get(instrument.instrument_id, 0)}, "
                        f"last_update={sink.last_update.get(instrument.instrument_id, 'NO LIVE TICKS YET')}"
                        for instrument in instruments.values()
                    )
                    + f"; observations={sink.observations_written}; 15M candles={sink.candles_written['15M']}; "
                    + f"1H candles={sink.candles_written['1H']}; reconnects={stream.stats.reconnect_count}"
                ),
                checks="read-only Lightstreamer runtime completed",
                warnings="; ".join(stream.stats.warnings) if stream.stats.warnings else "NONE",
                not_verified="A market with zero ticks has connection/subscription evidence but no real price-update evidence; LIVE streaming PASS was not claimed",
                output="; ".join(f"{candidate.market_name}\t{candidate.epic}" for candidate in selected),
                secrets=(client.settings.api_key, client.settings.username, client.settings.password, session.cst, session.security_token),
            )
            print("Streaming runtime completed (read-only)")
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
