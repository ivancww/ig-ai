from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = Path(os.environ.get("IGAI_STATE_DIR", str(PROJECT_DIR / ".igai"))).expanduser()
STATE_DIR = DEFAULT_STATE_DIR
CODEX_REPORT = STATE_DIR / "codex-report.txt"
TERMINAL_REPORT = STATE_DIR / "terminal-report.txt"
RUNTIME_RECORD = STATE_DIR / "stream-runtime.json"
UNIFIED_REPORT = Path(os.environ.get("IGAI_REPORT_PATH", str(STATE_DIR / "igai-report.txt"))).expanduser()


def _state_file(configured: Path, name: str) -> Path:
    """Follow a redirected STATE_DIR unless a file path was redirected explicitly."""
    return STATE_DIR / name if configured.parent == DEFAULT_STATE_DIR else configured


def _safe_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    lines = []
    sensitive_names = (
        "ig_api_key", "ig_username", "ig_password", "api_key", "username", "password",
        "cst", "x-security-token", "ls_password", "lightstreamer_password", "access_token",
        "refresh_token", "authorization", "session_id",
    )
    for line in text.splitlines():
        lower = line.lower()
        if any(name in lower for name in sensitive_names):
            lines.append("[REDACTED SENSITIVE OUTPUT]")
        else:
            lines.append(line)
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    # Tests and embedded callers may redirect an individual report path.  The
    # write target, rather than the module's default state directory, owns the
    # parent-directory requirement.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def update_terminal_report(
    *,
    command: str,
    account_type: str = "UNKNOWN",
    authentication: str = "NOT VERIFIED",
    market_discovery: str = "NOT RUN",
    streaming: str = "NOT RUN",
    runtime_outcome: str = "NOT RUN",
    connection_established: str = "NOT VERIFIED",
    session_established: str = "NOT VERIFIED",
    subscriptions_accepted: str = "NOT VERIFIED",
    real_price_updates: str = "NOT VERIFIED",
    final_state: str = "NOT VERIFIED",
    checks: str = "NOT RUN",
    warnings: str = "NONE",
    not_verified: str = "None stated",
    output: str = "",
    details: dict[str, object] | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    account = account_type.upper() if account_type.upper() in {"LIVE", "DEMO"} else "DEMO"
    body = "\n".join(
        [
            "Command executed: " + _safe_text(command, secrets),
            "Timestamp (UTC): " + datetime.now(UTC).isoformat(),
            "IG account environment: " + account,
            "Authentication: " + _safe_text(authentication, secrets),
            "Market discovery: " + _safe_text(market_discovery, secrets),
            "Streaming: " + _safe_text(streaming, secrets),
            "Runtime outcome: " + _safe_text(runtime_outcome, secrets),
            "Connection established: " + _safe_text(connection_established, secrets),
            "Session established: " + _safe_text(session_established, secrets),
            "Subscriptions accepted: " + _safe_text(subscriptions_accepted, secrets),
            "Real price updates received: " + _safe_text(real_price_updates, secrets),
            "Final state: " + _safe_text(final_state, secrets),
            "Tests/runtime checks: " + _safe_text(checks, secrets),
            "Warnings/errors: " + _safe_text(warnings, secrets),
            "Anything not verified: " + _safe_text(not_verified, secrets),
        ]
    )
    if details:
        body += "\n\nRuntime details:\n" + "\n".join(
            f"{_safe_text(key, secrets)}: {_safe_text(value, secrets)}"
            for key, value in details.items()
        )
    if output.strip():
        body += "\n\nSafe command output:\n" + _safe_text(output, secrets)
    _write(_state_file(TERMINAL_REPORT, "terminal-report.txt"), body)
    render_report()


def update_codex_report(text: str) -> None:
    _write(_state_file(CODEX_REPORT, "codex-report.txt"), _safe_text(text))


def write_runtime_record(record: dict) -> None:
    """Persist a safe lifecycle marker; RUNNING remains if the VM dies externally."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _state_file(RUNTIME_RECORD, "stream-runtime.json").write_text(
        json.dumps(record, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def render_report() -> str:
    secrets = tuple(
        os.environ.get(name, "") for name in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD", "CST", "X-SECURITY-TOKEN")
    )
    codex_report = _state_file(CODEX_REPORT, "codex-report.txt")
    terminal_report = _state_file(TERMINAL_REPORT, "terminal-report.txt")
    codex = (
        _safe_text(codex_report.read_text(encoding="utf-8"), secrets).strip()
        if codex_report.exists()
        else "Not available."
    )
    terminal = (
        _safe_text(terminal_report.read_text(encoding="utf-8"), secrets).strip()
        if terminal_report.exists()
        else "Not available."
    )
    report = (
        "=== CODEX / DEVELOPMENT REPORT ===\n\n"
        + codex
        + "\n\n=== TERMINAL / RUNTIME REPORT ===\n\n"
        + terminal
        + "\n"
    )
    UNIFIED_REPORT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    UNIFIED_REPORT.write_text(report, encoding="utf-8")
    return report


def _run_and_record(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=PROJECT_DIR, text=True, capture_output=True, check=False)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    update_terminal_report(
        command=" ".join(command),
        account_type=os.environ.get("IG_ACCOUNT_TYPE", "DEMO"),
        authentication="NOT RUN",
        checks=f"exit code {completed.returncode}",
        warnings="Command failed" if completed.returncode else "NONE",
        not_verified="IG authentication and market discovery were not run by this command",
        output=output,
        secrets=tuple(os.environ.get(name, "") for name in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD")),
    )
    sys.stdout.write(render_report())
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="igai-report")
    parser.add_argument("--run", nargs=argparse.REMAINDER, help="run a safe check and record its output")
    args = parser.parse_args(argv)
    if args.run:
        if args.run[0] in {"", "--"}:
            args.run.pop(0)
        if not args.run:
            parser.error("--run requires a command")
        return _run_and_record(args.run)
    sys.stdout.write(render_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
