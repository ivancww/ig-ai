#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT=ig-ai-20261005-15120
VM=ig-ai-prod
ZONE=us-central1-a
EXPECTED_SHA="${IGAI_APPROVED_SHA:-}"
REPO=https://github.com/ivancww/ig-ai.git
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_SCRIPT="$SCRIPT_DIR/deploy/ig-ai-remote-deploy.sh"

die() { echo "ERROR: $*" >&2; exit 1; }
for command_name in git gcloud tar mktemp sha256sum python3; do command -v "$command_name" >/dev/null 2>&1 || die "missing $command_name"; done
test -n "$EXPECTED_SHA" || die "set IGAI_APPROVED_SHA to the reviewed full commit SHA"
test "$EXPECTED_SHA" = "${EXPECTED_SHA//[^0-9a-f]/}" || die "approved source SHA is invalid"
test "${#EXPECTED_SHA}" = 40 || die "approved source SHA must be a full commit SHA"
test -r "$REMOTE_SCRIPT" || die "remote deployment script is missing"
bash -n "$REMOTE_SCRIPT" || die "remote deployment script has invalid syntax"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if test -n "$REPO_ROOT"; then
  test -z "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" || die "local source tree is not clean"
fi

run_remote() {
  local run_id="$1" remote_bundle="$2" rollback_path="$3" bundle_sha256="$4"
  gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap \
    --command="sudo env EXPECTED_SHA='$EXPECTED_SHA' BUNDLE_SHA256='$bundle_sha256' RUN_ID='$run_id' REMOTE_BUNDLE='$remote_bundle' ROLLBACK_PATH='$rollback_path' IGAI_ROOT=/opt/ig-ai IGAI_VENV=/opt/ig-ai/.venv IGAI_DB=/var/lib/ig-ai/data/ig_ai.sqlite3 IGAI_STATE_DIR=/var/lib/ig-ai/state IGAI_BACKUP_ROOT=/var/backups/ig-ai IGAI_UNIT_DIR=/etc/systemd/system IGAI_ENV_DIR=/etc/ig-ai IGAI_STAGE=/tmp/ig-ai-stage-$run_id IGAI_READINESS_TIMEOUT=120 bash -s" < "$REMOTE_SCRIPT"
}

if [[ $# -gt 0 && "$1" == "--rollback" ]]; then
  [[ $# == 2 ]] || die "usage: $0 --rollback /var/backups/ig-ai/<run-id>"
  case "$2" in /var/backups/ig-ai/*) ;; *) die "rollback path must be under /var/backups/ig-ai" ;; esac
  run_id="$(basename "$2")"
  run_remote "$run_id" "" "$2" ""
  exit 0
fi
[[ $# == 0 ]] || die "usage: $0 [--rollback /var/backups/ig-ai/<run-id>]"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
WORK="$(mktemp -d /tmp/ig-ai-deploy.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
CHECKOUT="$WORK/source"
BUNDLE="$WORK/application.tar.gz"
REMOTE_BUNDLE="/tmp/ig-ai-production-$RUN_ID.tar.gz"

echo "== Verify pinned main source =="
remote_sha="$(git ls-remote "$REPO" refs/heads/main | awk '{print $1}')"
test "$remote_sha" = "$EXPECTED_SHA" || die "main is not the approved target SHA"
git clone --filter=blob:none --no-checkout "$REPO" "$CHECKOUT" >/dev/null
git -C "$CHECKOUT" checkout --detach "$EXPECTED_SHA" >/dev/null
test "$(git -C "$CHECKOUT" rev-parse HEAD)" = "$EXPECTED_SHA" || die "source pin failed"
git -C "$CHECKOUT" diff --quiet || die "source checkout is dirty"
python3 - "$CHECKOUT" "$EXPECTED_SHA" <<'PY'
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

checkout = Path(sys.argv[1])
source_sha = sys.argv[2]
project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
lock = tomllib.loads((checkout / "uv.lock").read_text(encoding="utf-8"))
streaming_version = next(
    package["version"]
    for package in lock.get("package", [])
    if package.get("name") == "lightstreamer-client-lib"
)
web = checkout / "web"
web_files = {
    str(path.relative_to(web)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(web.rglob("*"))
    if path.is_file()
}
service_worker = (web / "assets" / "sw.js").read_text(encoding="utf-8")
cache_match = re.search(r'''CACHE\s*=\s*['"]([^'"]+)''', service_worker)
assert cache_match, "service-worker cache version is missing"
(checkout / "RELEASE-MANIFEST.json").write_text(
    json.dumps(
        {
            "manifest_version": 1,
            "source_sha": source_sha,
            "package_version": project["project"]["version"],
            "lightstreamer_version": streaming_version,
            "cache_version": cache_match.group(1),
            "web_files": web_files,
        },
        sort_keys=True,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
tar -C "$CHECKOUT" -czf "$BUNDLE" src web bin pyproject.toml uv.lock RELEASE-MANIFEST.json
tar -tzf "$BUNDLE" | grep -F 'src/ig_ai/' >/dev/null || die "source missing from bundle"
tar -tzf "$BUNDLE" | grep -F 'web/index.html' >/dev/null || die "Web missing from bundle"
tar -tzf "$BUNDLE" | grep -Fx 'RELEASE-MANIFEST.json' >/dev/null || die "release manifest missing from bundle"
if tar -tzf "$BUNDLE" | grep -F 'igai-report.txt' >/dev/null; then die "runtime report included in bundle"; fi
BUNDLE_SHA256="$(sha256sum "$BUNDLE" | awk '{print $1}')"

echo "== Upload approved bundle =="
gcloud compute scp "$BUNDLE" "$VM:$REMOTE_BUNDLE" --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap >/dev/null
echo "== Execute remote transaction =="
run_remote "$RUN_ID" "$REMOTE_BUNDLE" "" "$BUNDLE_SHA256"
echo "READY: deployment completed and post-deployment checks passed"
