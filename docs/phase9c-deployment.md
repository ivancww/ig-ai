# Phase 9C controlled VM deployment readiness

Phase 9C prepares, but does not perform, deployment to one small Linux
Google Compute Engine VM. It creates no Google Cloud resources and does not
open the web viewer to the Internet.

## Services

Install the existing application under `/opt/ig-ai` and run it as a
least-privilege `igai` user. Install both templates:

- `deploy/ig-ai.service.example` as `ig-ai.service` for the existing
  Phase 9A runtime/backend.
- `deploy/ig-ai-web.service.example` as `ig-ai-web.service` for the Phase 9B
  viewer.

Both services restart on failure, use SIGTERM, write to journald, and do not
run as root. The web service reads the same SQLite database as the backend.
Use `journalctl -u ig-ai -f` and `journalctl -u ig-ai-web -f` for logs.

## Environment and persistence

Copy `deploy/ig-ai.env.example` to `/etc/ig-ai/ig-ai.env`, provide the
existing IG credentials privately, and apply restrictive permissions such as
owner `root`, group `igai`, mode `0640`. Copy
`deploy/ig-ai-web.env.example` to `/etc/ig-ai/ig-ai-web.env` with mode `0600`.
The web environment contains no IG credentials.

Set `IG_DATABASE_PATH` to the persistent path
`/var/lib/ig-ai/data/ig_ai.sqlite3`. Ensure `/var/lib/ig-ai/data` is owned by
`igai`. SQLite WAL mode remains in use. First boot may use `ig-ai db-init` on
that path; it creates missing schema but does not overwrite an existing
database. Do not place the database under `/tmp`, the build checkout, or a
disposable release directory.

## Network and HTTPS boundary

The Python web server binds to `127.0.0.1` by default and accepts a
configurable loopback port via `IGAI_WEB_HOST` and `IGAI_WEB_PORT`. Do not
open that port in a firewall or security group. The future controlled
topology is:

`Browser/PWA → HTTPS reverse proxy → http://127.0.0.1:8080`

Domain, certificate, firewall, and TLS termination configuration are not
part of this PR. No domain is assumed and TLS verification is not disabled.

## Readiness check and boundaries

After both services are deliberately installed, run
`deploy/ig-ai-deployment-check.example` on the VM. It checks active systemd
services, read-only SQLite access, a current persisted heartbeat, visible
market rows, the persisted IG connection state, and local API reachability.
It does not claim that IG LIVE is healthy unless the persisted runtime state
provides that evidence, and it prints no secret values.

This branch is **ENGINEERING READY** only. VM CREATED: NO. 24/7 VM VALIDATED: NO.
HTTPS VALIDATED: NO. REAL HONOR DEVICE VALIDATED: NO. The application
remains read-only; no order, deal, position, account, or cloud-control
endpoint is added.
