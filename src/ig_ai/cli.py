from __future__ import annotations

import argparse
import logging

from .config import Settings
from .database import Database
from .discovery import discover_markets
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
    if args.command == "db-init":
        settings = Settings.from_env(require_credentials=False)
        database = Database(settings.database_path)
        database.close()
        print(f"Database initialized at {settings.database_path}")
        return 0
    client = IGRestClient(Settings.from_env())
    if args.command == "rest-check":
        client.authenticate()
        print("IG authentication succeeded (read-only verification)")
    elif args.command == "discover":
        for candidate in discover_markets(client):
            print(candidate)
    return 0
