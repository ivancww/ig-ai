#!/usr/bin/env bash
set -Eeuo pipefail

# All paths are supplied explicitly by the production wrapper or by the
# disposable acceptance harness. This script never discovers production paths.
ROOT="$IGAI_ROOT"
VENV="$IGAI_VENV"
DB="$IGAI_DB"
STATE_DIR="$IGAI_STATE_DIR"
BACKUP_ROOT="$IGAI_BACKUP_ROOT"
UNIT_DIR="$IGAI_UNIT_DIR"
ENV_DIR="$IGAI_ENV_DIR"
UNIT="$UNIT_DIR/ig-ai.service"
WEB_UNIT="$UNIT_DIR/ig-ai-web.service"
ENV="$ENV_DIR/ig-ai.env"
WEB_ENV="$ENV_DIR/ig-ai-web.env"
RUN_ID="$RUN_ID"
B="$BACKUP_ROOT/$RUN_ID"
S="$IGAI_STAGE"
REMOTE_BUNDLE="$REMOTE_BUNDLE"
ROLLBACK_PATH="$ROLLBACK_PATH"
READINESS_TIMEOUT="$IGAI_READINESS_TIMEOUT"
HEARTBEAT_MAX_AGE="${IGAI_HEARTBEAT_MAX_AGE:-}"
if test -n "$ROLLBACK_PATH"; then B="$ROLLBACK_PATH"; fi

die() {
  echo "REMOTE ERROR: $*" >&2
  if test "${rollback_in_progress-0}" = 1; then return 1; fi
  exit 1
}
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing deployment dependency: $1"; }
test "$(id -u)" = 0 || die "remote deployment must run as root"
test -n "${EXPECTED_SHA:-}" || die "approved source SHA is required"
test "${EXPECTED_SHA}" = "${EXPECTED_SHA//[^0-9a-f]/}" || die "approved source SHA is invalid"
test "${#EXPECTED_SHA}" = 40 || die "approved source SHA must be a full commit SHA"
for command_name in rsync curl ss tailscale awk tail grep find install systemctl python3 seq cp sort cmp id stat sha256sum tar getent runuser dirname date mkdir rm sleep mktemp; do require_cmd "$command_name"; done
id igai >/dev/null 2>&1 || die "missing runtime user: igai"
getent group igai >/dev/null 2>&1 || die "missing runtime group: igai"
test -x "$VENV/bin/python" && test -x "$VENV/bin/ig-ai" && test -x "$VENV/bin/ig-ai-web" || die "virtual environment is incomplete"
"$VENV/bin/python" -m pip --version >/dev/null || die "venv pip is unavailable"
"$VENV/bin/python" - <<'PY' || die "venv Python must be 3.12 or newer"
import sys
assert sys.version_info >= (3, 12)
PY
test -r "$UNIT" && test -r "$WEB_UNIT" && test -r "$ENV" && test -r "$WEB_ENV" || die "deployment files are missing"
test -r "$DB" && test -d "$ROOT/src" && test -d "$ROOT/web" && test -d "$ROOT/bin" || die "application/database paths are incomplete"

ensure_state_dir() {
  if test ! -e "$STATE_DIR"; then install -d -o igai -g igai -m 0750 "$STATE_DIR"; fi
  test -d "$STATE_DIR" || die "runtime state path is not a directory"
  test "$(stat -c '%U:%G' "$STATE_DIR")" = igai:igai || die "runtime state ownership is not igai:igai"
  test "$(stat -c '%a' "$STATE_DIR")" = 750 || die "runtime state mode is not 750"
}
unit_value() { awk -F= -v key="$2" '$1 == key { value=$2 } END { print value }' "$1"; }
verify_runtime_identity() {
  for unit in "$UNIT" "$WEB_UNIT"; do
    test "$(unit_value "$unit" User)" = igai || die "unit does not run as igai: $unit"
    test "$(unit_value "$unit" Group)" = igai || die "unit does not run as group igai: $unit"
    grep -Fx 'ReadWritePaths=/var/lib/ig-ai' "$unit" >/dev/null || die "unit lacks runtime write path: $unit"
  done
  data_dir="$(dirname "$DB")"
  test "$(stat -c '%U:%G' "$data_dir")" = igai:igai || die "database directory ownership is not igai:igai"
  test "$(stat -c '%U:%G' "$DB")" = igai:igai || die "database ownership is not igai:igai"
  runuser -u igai -- test -r "$DB" || die "igai cannot read the database"
  runuser -u igai -- test -w "$data_dir" || die "igai cannot write the database directory"
  runuser -u igai -- test -w "$STATE_DIR" || die "igai cannot write runtime state"
}
read_env_value() { awk -F= -v key="$2" '$1 == key { value=$2 } END { print value }' "$1"; }
test "$(read_env_value "$WEB_ENV" IGAI_WEB_HOST)" = 127.0.0.1 || die "web is not loopback"
test "$(read_env_value "$WEB_ENV" IGAI_WEB_PORT)" = 8080 || die "unexpected web port"
test "$(read_env_value "$ENV" IG_DATABASE_PATH)" = "$DB" || die "backend database path changed"
test "$(read_env_value "$WEB_ENV" IG_DATABASE_PATH)" = "$DB" || die "web database path changed"
test "$(read_env_value "$ENV" IGAI_STATE_DIR)" = "$STATE_DIR" || die "backend runtime state path changed"
test "$(read_env_value "$ENV" IGAI_REPORT_PATH)" = "$STATE_DIR/igai-report.txt" || die "backend report path changed"
backend_heartbeat_seconds="$(read_env_value "$ENV" IGAI_SERVICE_HEARTBEAT_SECONDS)"
web_heartbeat_seconds="$(read_env_value "$WEB_ENV" IGAI_SERVICE_HEARTBEAT_SECONDS)"
backend_stale_seconds="$(read_env_value "$ENV" IGAI_STALE_DATA_SECONDS)"
web_stale_seconds="$(read_env_value "$WEB_ENV" IGAI_STALE_DATA_SECONDS)"
test -n "$backend_heartbeat_seconds" && test "$backend_heartbeat_seconds" = "$web_heartbeat_seconds" || die "heartbeat contract differs between services"
test -n "$backend_stale_seconds" && test "$backend_stale_seconds" = "$web_stale_seconds" || die "stale-data contract differs between services"
if test -z "$HEARTBEAT_MAX_AGE"; then
  HEARTBEAT_MAX_AGE="$("$VENV/bin/python" - "$backend_heartbeat_seconds" <<'PY'
import sys
print(float(sys.argv[1]) * 3)
PY
)"
fi
"$VENV/bin/python" - "$HEARTBEAT_MAX_AGE" <<'PY' || die "heartbeat contract is invalid"
import sys
assert float(sys.argv[1]) > 0
PY
if test -z "$ROLLBACK_PATH"; then
  systemctl is-active --quiet ig-ai.service || die "backend was not active before deployment"
  systemctl is-active --quiet ig-ai-web.service || die "web was not active before deployment"
fi
tailscale status >/dev/null || die "tailscale is unavailable/not connected"
serve_before="$(tailscale serve status 2>&1)" || die "cannot inspect Tailscale Serve"
funnel_before="$(tailscale funnel status 2>&1 || true)"
if grep -Eiq 'funnel (on|enabled)|enabled.*funnel|funnel.*enabled' <<<"$funnel_before"; then die "Tailscale Funnel is enabled"; fi
ensure_state_dir
verify_runtime_identity

relevant_services="$(systemctl list-unit-files --type=service --no-legend --no-pager | awk '{print $1}' | grep -E '^(ig-ai|igai|.*self[-_]monitor|.*selfmonitor).*\.service$' | sort -u || true)"
relevant_timers="$(systemctl list-unit-files --type=timer --no-legend --no-pager | awk '{print $1}' | grep -E '^(ig-ai|igai|.*self[-_]monitor|.*selfmonitor).*\.timer$' | sort -u || true)"
grep -Fx ig-ai.service <<<"$relevant_services" >/dev/null || die "backend service was not discovered"
grep -Fx ig-ai-web.service <<<"$relevant_services" >/dev/null || die "web service was not discovered"
active_services="$(for unit in $relevant_services; do if systemctl is-active "$unit" 2>/dev/null | grep -Fx active >/dev/null; then echo "$unit"; fi; done || true)"
active_timers="$(for unit in $relevant_timers; do if systemctl is-active "$unit" 2>/dev/null | grep -Fx active >/dev/null; then echo "$unit"; fi; done || true)"
restore_services="$active_services"
restore_timers="$active_timers"

write_unit_manifest() {
  local output="$1"
  : > "$output"
  for unit in $relevant_services $relevant_timers; do
    printf '%s\t%s\t%s\t%s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)" "$(systemctl is-enabled "$unit" 2>/dev/null || true)" "$(systemctl cat "$unit" 2>/dev/null | sha256sum | awk '{print $1}')" >> "$output"
  done
}
mkdir -p "$B"
chmod 700 "$B"
marker="$(date -u +%s)"
if test -z "$ROLLBACK_PATH"; then
  write_unit_manifest "$B/systemd-before.tsv"
  printf '%s\n' "$serve_before" > "$B/tailscale-serve-before.txt"
  printf '%s\n' "$funnel_before" > "$B/tailscale-funnel-before.txt"
  printf '%s\n' "$active_services" > "$B/active-services.before.txt"
  printf '%s\n' "$active_timers" > "$B/active-timers.before.txt"
  printf '%s\n' "$marker" > "$B/deployment-marker.txt"
else
  test -f "$B/systemd-before.tsv" && test -f "$B/tailscale-serve-before.txt" && test -f "$B/tailscale-funnel-before.txt" || die "rollback state artifacts are incomplete"
  serve_before="$(cat "$B/tailscale-serve-before.txt")"
  funnel_before="$(cat "$B/tailscale-funnel-before.txt")"
fi

manifest() {
  "$VENV/bin/python" - "$ROOT" "$VENV" "$1" <<'PY'
import hashlib, importlib.metadata as m, json, sys
from pathlib import Path
import ig_ai
from ig_ai.web import WEB_DIR
root, venv, output = map(Path, sys.argv[1:])
web = Path(WEB_DIR).resolve()
assert web.is_relative_to((venv / "share" / "ig-ai" / "web").resolve())
files = {
    str(path.relative_to(web)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(web.rglob("*"))
    if path.is_file()
}
assert files
packages = sorted(
    {distribution.metadata["Name"].lower(): distribution.version for distribution in m.distributions()}.items()
)
Path(output).write_text(json.dumps({
    "python": sys.version, "python_executable": sys.executable,
    "ig_ai_version": m.version("ig-ai"),
    "lightstreamer_version": m.version("lightstreamer-client-lib"),
    "ig_ai_module": str(Path(ig_ai.__file__).resolve()),
    "web_dir": str(web), "web_files": files, "packages": packages,
}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
}
verify_installation() {
  manifest "$2"
  "$VENV/bin/python" - "$1" "$2" "${3-}" "${4-}" <<'PY'
import hashlib, json, sys
from pathlib import Path
source, manifest = map(Path, sys.argv[1:3])
data = json.loads(manifest.read_text())
assert Path(data["ig_ai_module"]).is_relative_to((source / "src").resolve())
assert data["lightstreamer_version"] == "2.2.3"
source_files = {
    str(path.relative_to(source / "web"))
    for path in (source / "web").rglob("*")
    if path.is_file()
}
assert set(data["web_files"]) == source_files
for name, digest in data["web_files"].items():
    assert hashlib.sha256((Path(data["web_dir"]) / name).read_bytes()).hexdigest() == digest
    assert hashlib.sha256((source / "web" / name).read_bytes()).hexdigest() == digest
expected_path = Path(sys.argv[3]) if sys.argv[3] else None
if expected_path:
    expected = json.loads(expected_path.read_text())
    assert expected["package_version"] == data["ig_ai_version"]
    assert expected["lightstreamer_version"] == data["lightstreamer_version"]
    assert expected["web_files"] == data["web_files"]
    if sys.argv[4]:
        assert expected["source_sha"] == sys.argv[4]
PY
}
step8() {
  "$VENV/bin/python" - "$1" "$2" <<'PY'
import json, sqlite3, sys
from pathlib import Path
db, output = sys.argv[1:]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
tables = ("forward_snapshots", "forward_snapshot_status", "forward_outcomes")
result = {"counts": {}, "rows": {}}
for table in tables:
    assert c.execute("select 1 from sqlite_master where name=?", (table,)).fetchone()
    result["counts"][table] = c.execute(f"select count(*) from {table}").fetchone()[0]
result["rows"]["snapshots"] = [list(r) for r in c.execute("select snapshot_id,source_identity,provenance,model_version,reference_time,reference_price from forward_snapshots order by snapshot_id")]
result["rows"]["snapshot_status"] = [list(r) for r in c.execute("select snapshot_id,eligibility,reason from forward_snapshot_status order by snapshot_id")]
result["rows"]["outcomes"] = [list(r) for r in c.execute("select snapshot_id,horizon,status from forward_outcomes order by snapshot_id,horizon")]
c.close()
Path(output).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
PY
}
compare_step8() {
  "$VENV/bin/python" - "$1" "$2" <<'PY'
import json, sys
before, after = [json.load(open(p, encoding="utf-8")) for p in sys.argv[1:]]
for table, count in before["counts"].items():
    assert after["counts"].get(table, -1) >= count, table
for table, rows in before["rows"].items():
    assert all(row in after["rows"].get(table, []) for row in rows), table
PY
}
sqlite_backup() {
  "$VENV/bin/python" - "$DB" "$1" <<'PY'
import sqlite3, sys
source, destination = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
try: source.backup(destination)
finally: destination.close(); source.close()
PY
  step8 "$1" "$1.step8.json"
  sha256sum "$1" | awk '{print $1}' > "$1.sha256"
  "$VENV/bin/python" - "$1" "$1.sha256" <<'PY'
import sqlite3, sys
from pathlib import Path

database, digest = sys.argv[1:]
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
connection.close()
assert len(Path(digest).read_text(encoding="utf-8").strip()) == 64
PY
}
verify_backup() {
  test -s "$1" && test -s "$1.sha256" && test -s "$1.step8.json" || die "SQLite backup artifacts are incomplete"
  "$VENV/bin/python" - "$1" "$1.sha256" "$1.step8.json" "$B/step8.before.json" <<'PY'
import hashlib, json, sqlite3, sys
from pathlib import Path

database, digest, backup_step8, before_step8 = sys.argv[1:]
expected_digest = Path(digest).read_text(encoding="utf-8").strip()
actual_digest = hashlib.sha256(Path(database).read_bytes()).hexdigest()
assert actual_digest == expected_digest
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
connection.close()
assert json.loads(Path(backup_step8).read_text()) == json.loads(Path(before_step8).read_text())
PY
}
record_db() {
  : > "$1"
  for suffix in '' -wal -shm; do
    path="$DB$suffix"
    if test -e "$path"; then printf '%s\t%s\t%s\t%s\n' "$suffix" "$(stat -c '%i' "$path")" "$(stat -c '%s' "$path")" "$(sha256sum "$path" | awk '{print $1}')" >> "$1"; fi
  done
}
stop_all() {
  for unit in $relevant_timers $relevant_services; do
    if systemctl is-active "$unit" 2>/dev/null | grep -Fx active >/dev/null; then systemctl stop "$unit"; fi
  done
  for unit in $relevant_timers $relevant_services; do test "$(systemctl is-active "$unit" 2>/dev/null || true)" != active || die "unit did not stop: $unit"; done
}
install_project() {
  if command -v uv >/dev/null 2>&1; then uv pip install --python "$VENV/bin/python" -e "$1[streaming]" --no-deps
  else "$VENV/bin/python" -m pip install -e "$1[streaming]" --no-deps
  fi
}
wait_backend() {
  local deadline=$((SECONDS + READINESS_TIMEOUT)) generation require_generation="${1:-1}"
  while (( SECONDS < deadline )); do
    if systemctl is-active --quiet ig-ai.service && generation="$("$VENV/bin/python" - "$DB" "$marker" "$HEARTBEAT_MAX_AGE" "$require_generation" <<'PY'
import json, sqlite3, sys
from datetime import UTC, datetime
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = c.execute("select state_value_json from runtime_state where state_key='service'").fetchone()
c.close()
if not row: raise SystemExit(1)
v = json.loads(row[0])
try: started = datetime.fromisoformat(v["service_started_at"]).astimezone(UTC).timestamp()
except (KeyError, TypeError, ValueError): raise SystemExit(1)
try: heartbeat_age = (datetime.now(UTC) - datetime.fromisoformat(v["last_heartbeat_at"]).astimezone(UTC)).total_seconds()
except (KeyError, TypeError, ValueError): raise SystemExit(1)
if started < float(sys.argv[2]) - 5 or heartbeat_age > float(sys.argv[3]) or v.get("status") != "HEALTHY" or v.get("ig_connection") != "CONNECTED" or v.get("startup_failure"): raise SystemExit(1)
if sys.argv[4] == "1" and not v.get("runtime_generation"): raise SystemExit(1)
print(v.get("runtime_generation", ""))
PY
 )"; then printf '%s\n' "$generation" > "$B/backend-generation.txt"; return 0; fi
    sleep 1
  done
  die "backend readiness deadline exceeded"
}
wait_web() {
  local generation="${1-}" expected_source_sha="${2:-$EXPECTED_SHA}" deadline=$((SECONDS + READINESS_TIMEOUT)) health="$B/health.json"
  while (( SECONDS < deadline )); do
    if systemctl is-active --quiet ig-ai-web.service && curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8080/api/health > "$health"; then
      if "$VENV/bin/python" - "$health" "$generation" "$marker" "$HEARTBEAT_MAX_AGE" "$backend_stale_seconds" "$expected_source_sha" <<'PY'
import json, sys
from datetime import UTC, datetime
p = json.load(open(sys.argv[1], encoding="utf-8"))
s, m = p.get("service", {}), p.get("self_monitoring", {})
heartbeat = datetime.fromisoformat(s["last_heartbeat"]).astimezone(UTC)
assert (datetime.now(UTC) - heartbeat).total_seconds() <= float(sys.argv[4])
contract = p.get("health_contract", {})
assert contract.get("heartbeat_max_age_seconds") == float(sys.argv[4])
assert contract.get("stale_data_seconds") == float(sys.argv[5])
build = p.get("build", {})
assert build.get("status") == "VERIFIED"
assert build.get("source_sha") == sys.argv[6]
if sys.argv[2]:
    assert s.get("runtime_generation") == sys.argv[2] == m.get("runtime_generation")
    assert m.get("ready")
    assert datetime.fromisoformat(s["service_started_at"]).astimezone(UTC).timestamp() >= float(sys.argv[3]) - 5
    assert datetime.fromisoformat(m["generated_at"]).astimezone(UTC) >= datetime.fromisoformat(s["service_started_at"]).astimezone(UTC)
assert p.get("overall_status") in ("HEALTHY", "ATTENTION")
critical = [item for item in p.get("incidents", []) if item.get("status", "ACTIVE") == "ACTIVE" and item.get("severity") in ("DEGRADED", "CRITICAL")]
assert not critical, critical
assert p.get("ig", {}).get("connection_status") == "CONNECTED"
markets = p.get("markets", [])
assert len(markets) >= 3
live = 0
for market in markets:
    status = market.get("market_status") or market.get("provider_status")
    assert status not in (None, "UNKNOWN")
    if market.get("monitoring_state") == "MONITORING":
        assert market.get("latest_tick") and market.get("tick_age_seconds") is not None and market["tick_age_seconds"] <= float(sys.argv[5])
        live += 1
    elif status in ("OPEN", "TRADEABLE"):
        raise AssertionError("tradeable market is not monitoring")
assert live
PY
      then return 0; fi
    fi
    sleep 1
  done
  die "web readiness deadline exceeded"
}
start_previous() {
  local generation="" require_generation="${1:-1}" expected_source_sha="${2:-$EXPECTED_SHA}"
  if grep -Fx ig-ai.service <<<"$restore_services" >/dev/null; then
    if ! systemctl start ig-ai.service; then return 1; fi
    if ! wait_backend "$require_generation"; then return 1; fi
  fi
  if grep -Fx ig-ai-web.service <<<"$restore_services" >/dev/null; then
    if ! systemctl start ig-ai-web.service; then return 1; fi
    if test "$require_generation" = 1; then generation="$(cat "$B/backend-generation.txt")"; fi
    if ! wait_web "$generation" "$expected_source_sha"; then return 1; fi
  fi
  for unit in $restore_services; do
    if test "$unit" != ig-ai.service && test "$unit" != ig-ai-web.service; then if ! systemctl start "$unit"; then return 1; fi; fi
  done
  for unit in $restore_timers; do if ! systemctl start "$unit"; then return 1; fi; done
}
rollback_ready=0
rollback_in_progress=0
rollback() {
  rollback_in_progress=1
  if ! verify_backup "$B/database.sqlite3"; then return 1; fi
  if ! stop_all; then return 1; fi
  rollback_dir="$(mktemp -d /tmp/ig-ai-rollback.XXXXXX)"
  if ! tar -xzf "$B/application.tgz" -C "$rollback_dir"; then return 1; fi
  if ! rsync -a --delete --exclude=.venv --exclude=.env --exclude=data --exclude=.igai --exclude=igai-report.txt "$rollback_dir/" "$ROOT/"; then return 1; fi
  if ! cp --preserve=all "$B/ig-ai.service" "$UNIT"; then return 1; fi
  if ! cp --preserve=all "$B/ig-ai-web.service" "$WEB_UNIT"; then return 1; fi
  if ! cp --preserve=all "$B/ig-ai.env" "$ENV"; then return 1; fi
  if ! cp --preserve=all "$B/ig-ai-web.env" "$WEB_ENV"; then return 1; fi
  if ! systemctl daemon-reload; then return 1; fi
  if ! install_project "$ROOT"; then return 1; fi
  if ! verify_installation "$ROOT" "$B/package.rollback.json" "$ROOT/RELEASE-MANIFEST.json"; then return 1; fi
  if ! cmp -s "$B/package.before.json" "$B/package.rollback.json"; then die "rollback package mismatch"; return 1; fi
  if ! cmp -s "$B/unit-sha.txt" <(sha256sum "$UNIT" "$WEB_UNIT"); then die "rollback changed systemd units"; return 1; fi
  if ! cmp -s "$B/env-sha.txt" <(sha256sum "$ENV" "$WEB_ENV"); then die "rollback changed environment files"; return 1; fi
  previous_source_sha="$("$VENV/bin/python" - "$ROOT/RELEASE-MANIFEST.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["source_sha"])
PY
)" || return 1
  if ! start_previous 0 "$previous_source_sha"; then return 1; fi
  if ! step8 "$DB" "$B/step8.rollback.json"; then return 1; fi
  if ! compare_step8 "$B/step8.before.json" "$B/step8.rollback.json"; then return 1; fi
  write_unit_manifest "$B/systemd-rollback.tsv"
  if ! cmp -s "$B/systemd-before.tsv" "$B/systemd-rollback.tsv"; then die "rollback changed systemd state"; return 1; fi
  if ! cmp -s <(printf '%s\n' "$serve_before") <(tailscale serve status 2>&1); then die "rollback changed Tailscale Serve"; return 1; fi
  if ! cmp -s <(printf '%s\n' "$funnel_before") <(tailscale funnel status 2>&1 || true); then die "rollback changed Tailscale Funnel"; return 1; fi
  rm -rf "$rollback_dir"
  echo "ROLLBACK VERIFIED: source/package/Web/services/Step 8 restored" >&2
}
on_exit() {
  local rc=$?
  trap - EXIT
  if test "$rc" -ne 0 && test "$rollback_ready" = 1 && test "$rollback_in_progress" = 0; then
    if ! rollback; then echo "ROLLBACK FAILED: state is not verified" >&2; rc=70; fi
  fi
  if test -n "$REMOTE_BUNDLE"; then rm -f "$REMOTE_BUNDLE"; fi
  rm -rf "$S"
  exit "$rc"
}
trap on_exit EXIT

if test -n "$ROLLBACK_PATH"; then
  test -f "$B/application.tgz" && test -f "$B/package.before.json" && test -f "$B/step8.before.json" || die "rollback artifacts are incomplete"
  test -f "$B/active-services.before.txt" && test -f "$B/active-timers.before.txt" || die "rollback service-state artifacts are incomplete"
  restore_services="$(cat "$B/active-services.before.txt")"
  restore_timers="$(cat "$B/active-timers.before.txt")"
  rollback_ready=1
  rollback
  echo "ROLLBACK COMPLETE: $B"
  exit 0
fi

test -n "$REMOTE_BUNDLE" || die "REMOTE_BUNDLE is required"
rm -rf "$S"; mkdir -p "$S"
tar -xzf "$REMOTE_BUNDLE" -C "$S"
for item in src web bin pyproject.toml uv.lock RELEASE-MANIFEST.json; do test -e "$S/$item" || die "bundle missing $item"; done
if find "$S" -name igai-report.txt -print -quit | grep -F . >/dev/null; then die "report artifact in bundle"; fi
tar --exclude=.venv --exclude=.git --exclude=.env --exclude=data --exclude=.igai --exclude=igai-report.txt -C "$ROOT" -czf "$B/application.tgz" .
cp --preserve=all "$UNIT" "$WEB_UNIT" "$ENV" "$WEB_ENV" "$B/"
printf '%s\n' "$(sha256sum "$UNIT" "$WEB_UNIT")" > "$B/unit-sha.txt"
printf '%s\n' "$(sha256sum "$ENV" "$WEB_ENV")" > "$B/env-sha.txt"
manifest "$B/package.before.json"
test -r "$ROOT/RELEASE-MANIFEST.json" || die "active release manifest is missing"
verify_installation "$ROOT" "$B/package.before.json" "$ROOT/RELEASE-MANIFEST.json"
record_db "$B/database.before.tsv"
printf 'source_sha=%s\nrun_id=%s\n' "$EXPECTED_SHA" "$RUN_ID" > "$B/metadata.txt"
sqlite_backup "$B/database.sqlite3"
step8 "$DB" "$B/step8.before.json"
compare_step8 "$B/step8.before.json" "$B/database.sqlite3.step8.json"
rollback_ready=1

stop_all

rsync -a --delete "$S/src/" "$ROOT/src/"
rsync -a --delete "$S/web/" "$ROOT/web/"
rsync -a --delete "$S/bin/" "$ROOT/bin/"
install -m 0644 "$S/pyproject.toml" "$ROOT/pyproject.toml"
install -m 0644 "$S/uv.lock" "$ROOT/uv.lock"
install -m 0644 "$S/RELEASE-MANIFEST.json" "$ROOT/RELEASE-MANIFEST.json"
install_project "$ROOT"
verify_installation "$ROOT" "$B/package.after.json" "$ROOT/RELEASE-MANIFEST.json" "$EXPECTED_SHA"
start_previous
step8 "$DB" "$B/step8.after.json"
compare_step8 "$B/step8.before.json" "$B/step8.after.json"
write_unit_manifest "$B/systemd-after.tsv"
cmp -s "$B/systemd-before.tsv" "$B/systemd-after.tsv" || die "systemd configuration/state changed"
cmp -s <(printf '%s\n' "$serve_before") <(tailscale serve status 2>&1) || die "Tailscale Serve changed"
cmp -s <(printf '%s\n' "$funnel_before") <(tailscale funnel status 2>&1 || true) || die "Tailscale Funnel changed"
for endpoint in /api/dashboard /api/alerts /api/health /api/schedule; do curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:8080$endpoint" >/dev/null || die "API failed: $endpoint"; done
ss -ltn | grep -E '127\.0\.0\.1:8080|\[::1\]:8080' >/dev/null || die "loopback listener missing"
if ss -ltn | grep -E '0\.0\.0\.0:8080|\[::\]:8080' >/dev/null; then die "public listener detected"; fi
for endpoint in /api/orders /api/deals /api/positions; do code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080$endpoint")"; test "$code" = 404 || die "trading endpoint exists"; done
echo "DEPLOYMENT VERIFIED: package/Web, IG readiness, WAL-safe backup and Step 8"
