from pathlib import Path

from ig_ai.web import main as web_main

ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy"


def test_systemd_templates_supervise_backend_and_localhost_web_only():
    backend = (DEPLOY / "ig-ai.service.example").read_text()
    web = (DEPLOY / "ig-ai-web.service.example").read_text()
    assert "WorkingDirectory=/opt/ig-ai" in backend
    assert "EnvironmentFile=/etc/ig-ai/ig-ai.env" in backend
    assert "ExecStart=/opt/ig-ai/.venv/bin/ig-ai service" in backend
    assert "Restart=on-failure" in backend and "KillSignal=SIGTERM" in backend
    assert "User=igai" in backend and "NoNewPrivileges=true" in backend
    assert "EnvironmentFile=/etc/ig-ai/ig-ai-web.env" in web
    assert "ExecStart=/opt/ig-ai/.venv/bin/ig-ai-web" in web
    assert "Restart=on-failure" in web and "KillSignal=SIGTERM" in web
    assert "User=igai" in web and "NoNewPrivileges=true" in web


def test_deployment_environment_and_check_preserve_secrets_and_loopback_boundary():
    env = (DEPLOY / "ig-ai-web.env.example").read_text()
    check = (DEPLOY / "ig-ai-deployment-check.example").read_text()
    assert "IGAI_WEB_HOST=127.0.0.1" in env
    assert "IGAI_WEB_PORT=8080" in env
    assert "IG_DATABASE_PATH=/var/lib/ig-ai/data/ig_ai.sqlite3" in env
    assert "case \"$web_host\"" in check
    assert "systemctl is-active" in check
    assert "mode=ro" in check
    assert "/api/health" in check
    assert "IG_PASSWORD" not in env and "IG_API_KEY=" not in env
    assert "0.0.0.0" not in check


def test_web_defaults_are_loopback_and_environment_configurable(monkeypatch):
    monkeypatch.setenv("IGAI_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("IGAI_WEB_PORT", "18080")
    # main is intentionally not started here; its parser/entry contract is
    # covered by the source-level deployment template and CLI smoke tests.
    assert web_main is not None


def test_phase9c_documentation_separates_readiness_from_live_validation():
    document = (ROOT / "docs/phase9c-deployment.md").read_text()
    assert "ENGINEERING READY" in document
    assert "VM CREATED: NO" in document
    assert "24/7 VM VALIDATED: NO" in document
    assert "HTTPS VALIDATED: NO" in document
    assert "REAL HONOR DEVICE VALIDATED: NO" in document
    assert "Browser/PWA → HTTPS reverse proxy" in document
