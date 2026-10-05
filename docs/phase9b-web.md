# Phase 9B web/PWA surface

Run the local viewer with `ig-ai-web --host 127.0.0.1 --port 8080`. It reads
the existing SQLite read models and exposes `/api/dashboard`, `/api/alerts`,
`/api/health`, and `/api/schedule`. The browser never receives IG credentials
or session tokens and never opens SQLite directly.

The only web write is `POST /api/alerts/{alert_id}/read` (or `/unread`). It
updates `alert_ui_state`; immutable analytical `alerts` rows are untouched.
There are no order, deal, position, or other trading mutation endpoints.

The schedule page is deliberately a validated preview. `MARKET_HOURS` stays
unknown unless verified session rules exist, and this UI cannot stop the
service or VM. The service worker caches only the application shell; API
requests bypass the cache so an offline shell cannot represent old market
data as current.

`web/icons/` contains clearly marked development placeholders. The manifest
reserves 192/512, maskable 192/512, and favicon slots for replacement by the
approved production logo source.
