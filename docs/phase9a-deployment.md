# Phase 9A deployment/runbook

Phase 9A makes the existing read-only IG/Lightstreamer/candle/technical/pattern/direction/alert/decision stack suitable for a continuously supervised host. It does not provision a VM, place orders, or create an HTTP API.

## Install

1. Provision a host and a least-privilege OS user (for example `igai`).
2. Check out `main` or the reviewed Phase 9A release, create a virtual environment, and install `.[streaming]`.
3. Create `/etc/ig-ai/ig-ai.env` from `deploy/ig-ai.env.example`. Put credentials only in that root-readable environment file or another secret manager; never in Git.
4. Choose a writable database directory and ensure it is owned by the service user. SQLite remains WAL-mode and is not committed.
5. Install `deploy/ig-ai.service.example` as `/etc/systemd/system/ig-ai.service`, review `User`, `Group`, `WorkingDirectory`, and paths, then run `systemctl daemon-reload` and `systemctl enable --now ig-ai`.

## Operations

Use `journalctl -u ig-ai -f` for bounded host-managed logs. The process handles SIGTERM/SIGINT, stops the Lightstreamer client, flushes open candles as open, persists STOPPING state, and closes SQLite. systemd owns restart after failure and reboot; the application does not self-restart.

`ig-ai service-status` reads only persisted state and safely prints `UNKNOWN` or `NOT AVAILABLE` when evidence is absent. `ig-ai db-init` can initialize a new database without credentials. `ig-ai service` discovers provider-verified weekday cash instruments, starts the existing `PersistedStream`, and remains read-only.

`IGAI_MONITORING_MODE` supports `24_7`, `MARKET_HOURS`, and `CUSTOM`. `24_7` is the safe default. `MARKET_HOURS` has no fabricated official session rules and therefore reports `UNKNOWN`/does not analyze until verified rules are supplied. For `CUSTOM`, set required `IGAI_CUSTOM_WINDOWS` as semicolon-separated `DAYS HH:MM-HH:MM` windows, for example `1,2,3,4,5 09:00-17:00;6 10:00-12:00`. ISO weekdays are `1=Monday` through `7=Sunday`; `5 20:00-05:00` includes Friday evening through Saturday 05:00. Windows are evaluated in `IGAI_TIMEZONE`, validated at startup, persisted in `runtime_schedule`, and displayed by `service-status`.

## Readiness boundaries

Heartbeat writes are interval-bounded. A missing tick is not automatically a failure: market state distinguishes `SCHEDULED_OFF`, `STALE_DATA`, and `UNKNOWN`. Restored partial candles remain audit-only under the existing freshness rules, and no restored partial candle becomes eligible analytical or LIVE_FORWARD evidence. No automatic trading or IG order/position mutation is present.

Real 24/7 uptime validation requires a later supervised host run. Unit tests are deterministic and do not claim production uptime.
