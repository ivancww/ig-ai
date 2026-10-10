"""Execute the operator health check with representative API responses."""

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CHECK = ROOT / "deploy" / "ig-ai-deployment-check.example"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("healthy", [True, False], ids=["healthy", "stale-heartbeat"])
def test_deployment_check_executes_actual_health_script(healthy: bool, tmp_path: Path):
    database = tmp_path / "ig-ai.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("select 1")
    connection.close()
    now = datetime.now(UTC).isoformat()
    health = {
        "overall_status": "HEALTHY",
        "health_contract": {"heartbeat_max_age_seconds": 90.0, "stale_data_seconds": 900.0},
        "service": {
            "status": "HEALTHY",
            "ig_connection": "CONNECTED",
            "last_heartbeat": now if healthy else "2020-01-01T00:00:00+00:00",
        },
        "markets": [{"market": "US Tech 100"}, {"market": "Japan 225"}, {"market": "Hong Kong HS50"}],
    }
    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps(health), encoding="utf-8")

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "systemctl",
        "#!/bin/sh\nif test \"$1\" = is-active; then exit 0; fi\nexit 1\n",
    )
    _write_executable(
        fakebin / "curl",
        f"""#!/bin/sh
url=""
for argument do url="$argument"; done
case "$url" in
  */api/health) cat {health_path} ;;
  */api/dashboard) printf '{{}}\\n' ;;
  *) exit 22 ;;
esac
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fakebin}:{environment['PATH']}",
            "IGAI_SERVICE_NAME": "ig-ai",
            "IGAI_WEB_SERVICE_NAME": "ig-ai-web",
            "IGAI_WEB_HOST": "127.0.0.1",
            "IGAI_WEB_PORT": "8080",
            "IG_DATABASE_PATH": str(database),
            "IGAI_SERVICE_HEARTBEAT_SECONDS": "30",
            "IGAI_STALE_DATA_SECONDS": "900",
        }
    )
    result = subprocess.run(
        ["bash", str(CHECK)], env=environment, text=True, capture_output=True, check=False
    )
    if healthy:
        assert result.returncode == 0, result.stderr
        assert "dashboard API: reachable on loopback" in result.stdout
    else:
        assert result.returncode != 0
        assert "FAIL: heartbeat is stale" in result.stderr
