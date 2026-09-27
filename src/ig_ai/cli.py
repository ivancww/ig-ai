from __future__ import annotations

import argparse
import logging
import os

from .config import Settings
from .database import Database
from .discovery import discover_market_groups
from .exceptions import IGHTTPError
from .reporting import update_terminal_report
from .rest import IGRestClient
from .security import SecretRedactionFilter


def main() -> int:
    parser = argparse.ArgumentParser(prog="ig-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-init")
    commands.add_parser("discover")
    commands.add_parser("rest-check")
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
                for candidate in group.candidates:
                    line = (
                        f"  {candidate.market_name}\t{candidate.epic}\t"
                        f"{candidate.market_status or ''}\t{candidate.instrument_type or ''}\t"
                        f"{candidate.expiry or ''}\t{candidate.classification}\t"
                        f"eligible_primary={candidate.eligible_primary}\tverified={candidate.verified}"
                    )
                    safe_results.append(line)
                    print(line)
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
