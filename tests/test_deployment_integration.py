"""Disposable deployment transaction tests; no systemd, credentials, or IG network are used."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from ig_ai.database import Database

ROOT = Path(__file__).parents[1]
REMOTE = ROOT / "deploy" / "ig-ai-remote-deploy.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _prepare_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path, Database]:
    app = tmp_path / "opt" / "ig-ai"
    app.mkdir(parents=True)
    for name in ("src", "web", "bin", "pyproject.toml", "uv.lock"):
        source = ROOT / name
        destination = app / name
        shutil.copytree(source, destination) if source.is_dir() else shutil.copy2(source, destination)
    bundle_root = tmp_path / "bundle"
    shutil.copytree(app, bundle_root)
    (bundle_root / "web" / "assets" / "app.js").open("a").write("\n// VERSION_B\n")
    bundle = tmp_path / "application.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name in ("src", "web", "bin", "pyproject.toml", "uv.lock"):
            archive.add(bundle_root / name, arcname=name)

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    site = Path(subprocess.check_output([str(python), "-c", "import site; print(site.getsitepackages()[0])"], text=True).strip())
    (site / "ig_ai_test.pth").write_text(str(app / "src") + "\n", encoding="utf-8")
    for distribution, version in (("ig-ai", "0.1.0"), ("lightstreamer-client-lib", "2.2.3")):
        info = site / f"{distribution.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n", encoding="utf-8")
    installed_web = venv / "share" / "ig-ai" / "web"
    shutil.copytree(app / "web", installed_web)
    for entrypoint in ("ig-ai", "ig-ai-web"):
        _write_executable(venv / "bin" / entrypoint, "#!/bin/sh\nexit 0\n")

    database_path = tmp_path / "var" / "lib" / "ig-ai" / "data" / "ig_ai.sqlite3"
    database_path.parent.mkdir(parents=True)
    database = Database(database_path)
    now = datetime.now(UTC).isoformat()
    database.save_forward_snapshot({
        "snapshot_id": "step8-original", "timestamp": now, "reference_time": now,
        "market": "US Tech 100", "instrument_id": "EPIC", "epic": "EPIC",
        "source_identity": "ig_cfd|instrument=EPIC|epic=EPIC|market=US Tech 100|type=INDICES",
        "timeframe": "1H", "technical_state": {}, "pattern_state": {}, "direction_state": {},
        "reference_price": 100, "direction": "UP", "model_version": "test",
    })
    database.save_forward_outcomes("step8-original", {"1H": {"status": "PENDING"}})
    database.connection.execute("PRAGMA wal_autocheckpoint=0")
    database.save_runtime_state("wal-holder", {"status": "ACTIVE"})

    unit_dir = tmp_path / "etc" / "systemd" / "system"
    env_dir = tmp_path / "etc" / "ig-ai"
    unit_dir.mkdir(parents=True)
    env_dir.mkdir(parents=True)
    unit = "[Service]\nUser=igai\nGroup=igai\nReadWritePaths=/var/lib/ig-ai\nExecStart=/opt/ig-ai/.venv/bin/ig-ai service\n"
    (unit_dir / "ig-ai.service").write_text(unit, encoding="utf-8")
    (unit_dir / "ig-ai-web.service").write_text(unit, encoding="utf-8")
    state_dir = tmp_path / "var" / "lib" / "ig-ai" / "state"
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o750)
    (env_dir / "ig-ai.env").write_text(
        f"IG_DATABASE_PATH={database_path}\nIGAI_STATE_DIR={state_dir}\nIGAI_REPORT_PATH={state_dir / 'igai-report.txt'}\n",
        encoding="utf-8",
    )
    (env_dir / "ig-ai-web.env").write_text(f"IG_DATABASE_PATH={database_path}\nIGAI_WEB_HOST=127.0.0.1\nIGAI_WEB_PORT=8080\n", encoding="utf-8")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    state_file = tmp_path / "active.txt"
    state_file.write_text("ig-ai.service\nig-ai-web.service\n", encoding="utf-8")
    health_file = tmp_path / "health.json"
    provider_log = tmp_path / "provider.log"
    helper = tmp_path / "backend-ready.py"
    helper.write_text(
        """import json, os, sqlite3, sys\nfrom datetime import UTC, datetime\ndb, health, log = sys.argv[1:]\nnow = datetime.now(UTC).isoformat(); generation = 'fake-' + str(int(datetime.now(UTC).timestamp() * 1000000))\nheartbeat = '2020-01-01T00:00:00+00:00' if os.path.exists(os.environ.get('STALE_HEARTBEAT_MARKER', '')) else now\nmissing_self_monitor = os.path.exists(os.environ.get('MISSING_SELF_MONITOR_MARKER', ''))\nfor marker in (os.environ.get('STALE_HEARTBEAT_MARKER', ''), os.environ.get('MISSING_SELF_MONITOR_MARKER', '')):\n    if marker and os.path.exists(marker): os.unlink(marker)\nlegacy = os.environ.get('LEGACY_ROLLBACK') == '1' and os.path.exists(os.environ['UV_COUNT']) and int(open(os.environ['UV_COUNT']).read()) >= 2\nc = sqlite3.connect(db)\nvalue = {'status':'HEALTHY','service_started_at':now,'last_heartbeat_at':heartbeat,'ig_connection':'CONNECTED'}\nif not legacy: value['runtime_generation'] = generation\nc.execute(\"INSERT INTO runtime_state VALUES ('service', ?, ?) ON CONFLICT(state_key) DO UPDATE SET state_value_json=excluded.state_value_json, updated_at=excluded.updated_at\", (json.dumps(value), now))\nc.commit(); c.close()\nmarkets = [{'market':'US Tech 100','market_status':'TRADEABLE','monitoring_state':'MONITORING','latest_tick':now,'tick_age_seconds':0},{'market':'Japan 225','market_status':'TRADEABLE','monitoring_state':'MONITORING','latest_tick':now,'tick_age_seconds':0},{'market':'Hong Kong HS50','market_status':'CLOSED','monitoring_state':'MARKET_CLOSED','latest_tick':None,'tick_age_seconds':None}]\npayload = {'overall_status':'HEALTHY','service':{**value,'last_heartbeat':heartbeat},'ig':{'connection_status':'CONNECTED'},'markets':markets}\nif not legacy and not missing_self_monitor: payload['self_monitoring'] = {'ready':True,'runtime_generation':generation,'generated_at':now}\nopen(health,'w',encoding='utf-8').write(json.dumps(payload))\nopen(log,'a',encoding='utf-8').write('POST /session\\nGET /markets\\nGET /prices\\n')\n""",
        encoding="utf-8",
    )
    _write_executable(fakebin / "systemctl", f"""#!/bin/sh
state={state_file}
db={database_path}; health={health_file}; log={provider_log}; helper={helper}
echo systemctl "$@" >> {tmp_path}/systemctl.log
case "$1" in
  list-unit-files) echo 'ig-ai.service enabled'; echo 'ig-ai-web.service enabled'; exit 0;;
  is-enabled) echo enabled; exit 0;;
  cat) cat {unit_dir}/$2; exit 0;;
  is-active) test "$2" = --quiet && unit=$3 || unit=$2; grep -Fx "$unit" "$state" >/dev/null && {{ test "$2" = --quiet || echo active; exit 0; }}; test "$2" = --quiet || echo inactive; exit 3;;
  stop) if test -e {tmp_path}/stop-failure-once; then rm -f {tmp_path}/stop-failure-once; exit 1; fi; sed -i "/^$2$/d" "$state"; exit 0;;
  start) grep -Fx "$2" "$state" >/dev/null || echo "$2" >> "$state"; test "$2" = ig-ai.service && /usr/bin/python3 "$helper" "$db" "$health" "$log"; exit 0;;
  daemon-reload) exit 0;;
esac
exit 1
""")
    _write_executable(fakebin / "uv", f"""#!/bin/sh
if test -e {tmp_path}/fail-always; then exit 1; fi
if test -e {tmp_path}/fail-once; then rm -f {tmp_path}/fail-once; exit 1; fi
count="$(cat {tmp_path}/uv.count 2>/dev/null || echo 0)"; count=$((count + 1)); echo "$count" > {tmp_path}/uv.count
test "$count" -ge 2 && rm -f {tmp_path}/web-failure-persistent
rm -rf {installed_web}; mkdir -p {installed_web}; cp -a "$IGAI_ROOT/web/." {installed_web}/
if test -e {tmp_path}/web-asset-mismatch; then rm -f {tmp_path}/web-asset-mismatch; echo mismatch >> {installed_web}/icons/favicon.svg; fi
exit 0
""")
    _write_executable(fakebin / "curl", f"""#!/bin/sh
url="$(printf '%s\\n' "$@" | tail -n 1)"
case "$url" in
  */api/health)
    if test -e {tmp_path}/web-failure; then rm -f {tmp_path}/web-failure; exit 7; fi
    if test -e {tmp_path}/web-failure-persistent; then count="$(cat {tmp_path}/web-failure.count 2>/dev/null || echo 0)"; count=$((count + 1)); echo "$count" > {tmp_path}/web-failure.count; test "$count" -lt 100 && exit 7; fi
    if test -e {tmp_path}/web-delay; then count="$(cat {tmp_path}/web-delay.count 2>/dev/null || echo 0)"; count=$((count + 1)); echo "$count" > {tmp_path}/web-delay.count; test "$count" -lt 3 && exit 7; fi
    cat {health_file}; exit 0;;
  */orders|*/deals|*/positions) printf '%s\\n' "$@" | grep -F -- '-w' >/dev/null && echo 404; exit 0;;
  *) echo '{{}}'; exit 0;;
esac
""")
    _write_executable(fakebin / "ss", "#!/bin/sh\necho 'LISTEN 0 5 127.0.0.1:8080 0.0.0.0:*'\n")
    _write_executable(fakebin / "tailscale", "#!/bin/sh\ncase \"$1\" in status) exit 0;; serve) echo 'https://tailnet.example -> http://127.0.0.1:8080';; funnel) echo 'funnel off';; esac\n")
    _write_executable(fakebin / "rsync", """#!/usr/bin/python3
import shutil, sys
args = [x for x in sys.argv[1:] if not x.startswith('-')]
source, destination = args[-2].rstrip('/'), args[-1].rstrip('/')
if '--delete' in sys.argv:
    for child in shutil.os.listdir(destination):
        if child not in {'.venv', '.env', 'data', '.igai', 'igai-report.txt'}:
            path = shutil.os.path.join(destination, child)
            shutil.rmtree(path) if shutil.os.path.isdir(path) else shutil.os.unlink(path)
shutil.copytree(source, destination, dirs_exist_ok=True)
""")
    _write_executable(fakebin / "id", "#!/bin/sh\nif test \"$1\" = igai || test \"$1\" = -u; then test \"$1\" = -u && echo 0; exit 0; fi\nexec /usr/bin/id \"$@\"\n")
    _write_executable(fakebin / "getent", "#!/bin/sh\nif test \"$1\" = group && test \"$2\" = igai; then echo 'igai:x:999:'; exit 0; fi\nexec /usr/bin/getent \"$@\"\n")
    _write_executable(fakebin / "runuser", f"#!/bin/sh\nif test -e {tmp_path}/permission-denied; then exit 1; fi\nwhile test \"$1\" != --; do shift; done\nshift\nexec \"$@\"\n")
    _write_executable(fakebin / "stat", f"""#!/bin/sh
case "$*" in *%U:%G*{state_dir}|*%U:%G*{database_path}|*%U:%G*{database_path.parent}) echo igai:igai;; *%a*{state_dir}) echo 750;; *) exec /usr/bin/stat "$@";; esac
""")
    _write_executable(fakebin / "install", """#!/usr/bin/env bash
args=(); skip=0
for arg in "$@"; do if test "$skip" = 1; then skip=0; continue; fi; case "$arg" in -o|-g|-m) skip=1;; *) args+=("$arg");; esac; done
exec /usr/bin/install "${args[@]}"
""")
    env = os.environ.copy()
    env.update({
        "PATH": f"{fakebin}:/usr/local/bin:/usr/bin:/bin",
        "IGAI_ROOT": str(app), "IGAI_VENV": str(venv), "IGAI_DB": str(database_path),
        "IGAI_STATE_DIR": str(state_dir), "IGAI_BACKUP_ROOT": str(tmp_path / "backups"),
        "IGAI_UNIT_DIR": str(unit_dir), "IGAI_ENV_DIR": str(env_dir), "RUN_ID": "integration-run",
        "IGAI_STAGE": str(tmp_path / "stage"), "REMOTE_BUNDLE": str(bundle), "ROLLBACK_PATH": "",
        "IGAI_READINESS_TIMEOUT": "10", "IGAI_HEARTBEAT_MAX_AGE": "120", "EXPECTED_SHA": "test",
        "STALE_HEARTBEAT_MARKER": str(tmp_path / "stale-heartbeat"),
        "MISSING_SELF_MONITOR_MARKER": str(tmp_path / "missing-self-monitor"),
    })
    return env, app, database_path, provider_log, database


def _run_transaction(tmp_path: Path, *, failure: str | tuple[str, ...] | None = None) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    env, app, database, provider_log, wal_holder = _prepare_environment(tmp_path)
    failures = [failure] if isinstance(failure, str) else list(failure or ())
    for marker in failures:
        (tmp_path / marker).write_text("1", encoding="utf-8")
    if "missing-rsync" in failures:
        (tmp_path / "fakebin" / "rsync").unlink()
    if "legacy-rollback" in failures:
        env["LEGACY_ROLLBACK"] = "1"
        env["UV_COUNT"] = str(tmp_path / "uv.count")
    try:
        result = subprocess.run(["bash", str(REMOTE)], env=env, text=True, capture_output=True, timeout=30)
    finally:
        wal_holder.close()
    return result, app, database, provider_log


def test_disposable_deployment_package_start_readiness_and_wal_backup(tmp_path):
    result, app, database, provider_log = _run_transaction(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "VERSION_B" in (app / "web" / "assets" / "app.js").read_text()
    assert (tmp_path / "backups" / "integration-run" / "database.sqlite3.step8.json").exists()
    assert json.loads((tmp_path / "backups" / "integration-run" / "step8.after.json").read_text())["counts"]["forward_snapshots"] == 1
    assert "POST /session" in provider_log.read_text()
    assert "POST /orders" not in provider_log.read_text()
    assert "-wal\t" in (tmp_path / "backups" / "integration-run" / "database.before.tsv").read_text()


def test_disposable_package_failure_rolls_back_source_package_and_step8(tmp_path):
    result, app, database, provider_log = _run_transaction(tmp_path, failure="fail-once")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()
    reopened = Database(database)
    assert "step8-original" in reopened.connection.execute("SELECT snapshot_id FROM forward_snapshots").fetchone()[0]
    reopened.close()
    assert "POST /orders" not in provider_log.read_text()


def test_disposable_rollback_failure_is_fail_closed(tmp_path):
    result, _app, _database, _provider_log = _run_transaction(tmp_path, failure="fail-always")
    assert result.returncode == 70
    assert "ROLLBACK FAILED" in result.stderr


def test_missing_deployment_dependency_fails_before_service_stop(tmp_path):
    result, _app, _database, _provider_log = _run_transaction(tmp_path, failure="missing-rsync")
    assert result.returncode != 0
    assert "missing deployment dependency: rsync" in result.stderr
    assert (tmp_path / "active.txt").read_text().splitlines() == ["ig-ai.service", "ig-ai-web.service"]


def test_runtime_permission_denial_fails_before_service_stop(tmp_path):
    result, _app, _database, _provider_log = _run_transaction(tmp_path, failure="permission-denied")
    assert result.returncode != 0
    assert "igai cannot read the database" in result.stderr
    assert (tmp_path / "active.txt").read_text().splitlines() == ["ig-ai.service", "ig-ai-web.service"]


def test_web_readiness_delay_is_bounded_and_eventually_verified(tmp_path):
    result, _app, _database, _provider_log = _run_transaction(tmp_path, failure="web-delay")
    assert result.returncode == 0, result.stderr


def test_stale_backend_heartbeat_triggers_verified_rollback(tmp_path):
    result, app, _database, _provider_log = _run_transaction(tmp_path, failure="stale-heartbeat")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()


def test_missing_current_self_monitor_triggers_verified_rollback(tmp_path):
    result, app, _database, _provider_log = _run_transaction(tmp_path, failure="missing-self-monitor")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()


def test_web_startup_failure_triggers_verified_rollback(tmp_path):
    result, app, _database, _provider_log = _run_transaction(tmp_path, failure="web-failure-persistent")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()


def test_web_asset_mismatch_triggers_verified_rollback(tmp_path):
    result, app, _database, _provider_log = _run_transaction(tmp_path, failure="web-asset-mismatch")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()
    assert "mismatch" not in (app / "web" / "icons" / "favicon.svg").read_text()


def test_service_stop_failure_rolls_back_after_backup_is_ready(tmp_path):
    result, app, _database, _provider_log = _run_transaction(tmp_path, failure="stop-failure-once")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()
    assert (tmp_path / "active.txt").read_text().splitlines() == ["ig-ai.service", "ig-ai-web.service"]


def test_rollback_readiness_accepts_compatible_legacy_previous_generation(tmp_path):
    result, app, _database, _provider_log = _run_transaction(
        tmp_path, failure=("fail-once", "legacy-rollback")
    )
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()


def test_explicit_rollback_reuses_saved_predeployment_artifacts(tmp_path):
    env, app, _database, _provider_log, wal_holder = _prepare_environment(tmp_path)
    try:
        deployed = subprocess.run(
            ["bash", str(REMOTE)], env=env, text=True, capture_output=True, timeout=30
        )
    finally:
        wal_holder.close()
    assert deployed.returncode == 0, deployed.stderr
    env["REMOTE_BUNDLE"] = ""
    env["ROLLBACK_PATH"] = str(tmp_path / "backups" / "integration-run")
    rolled_back = subprocess.run(
        ["bash", str(REMOTE)], env=env, text=True, capture_output=True, timeout=30
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert "ROLLBACK VERIFIED" in rolled_back.stderr
    assert "VERSION_B" not in (app / "web" / "assets" / "app.js").read_text()
