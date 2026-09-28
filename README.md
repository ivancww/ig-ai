# ig-ai

Technical-first market analysis infrastructure. Phase 1 provides a secure, read-only IG market-data foundation; it does not place trades or claim that IG CFD prices are official cash-index prices.

## Architecture

`config` validates environment configuration, `rest` handles IG REST sessions and read-only market endpoints, `discovery` searches rather than guesses EPICs, `streaming` uses the official Lightstreamer Python Client SDK, `normalization` converts provider updates into UTC observations, `candles` aggregates forming and closed OHLC candles, `technical` calculates deterministic, versioned indicators from candle history, and `database` stores all read-only market data in SQLite. Phase 2A adds technical features and an outcome-recording foundation; it does not create signals, probabilities, orders, or AI predictions.

The internal model preserves the distinction between market name, IG EPIC, instrument type, and data source. It is ready for separate cash-index, futures, and CFD instruments later.

## Setup

Python 3.12+ is supported. Create a virtual environment and install the project with the development tools:

```bash
uv venv
uv pip install -e '.[dev]'
```

Copy `.env.example` to `.env` or export equivalent variables. Do not commit `.env`:

```bash
export IG_API_KEY='...'
export IG_USERNAME='...'
export IG_PASSWORD='...'
export IG_ACCOUNT_TYPE='LIVE'
```

The CLI validates credentials without printing them. `IG_ACCOUNT_TYPE` supports `DEMO` and `LIVE`; this application remains read-only in either mode. Optional settings include `IG_DATABASE_PATH`, `IG_REQUEST_TIMEOUT_SECONDS`, `IG_DISCOVERY_DETAIL_BUDGET`, `IG_STREAM_RECONNECT_SECONDS`, and `IG_MARKET_TIMEZONE`.

## Commands

```bash
ig-ai db-init
ig-ai rest-check       # read-only authentication check
ig-ai discover         # searches US Tech 100, Japan 225, Hong Kong HS50
ig-ai stream --duration 300  # three-market LIVE read-only Lightstreamer validation
ig-ai phase1-check      # offline final acceptance; does not start a LIVE run
ig-ai technical-status  # inspect the latest persisted feature record (no BUY/SELL output)
igai-report            # prints and refreshes the unified Codex/runtime report
igai-report --run pytest -q
igai-report --run ruff check .
```

The unified report is also saved as `igai-report.txt` for Cloud Shell Editor copy/paste. Runtime commands update only the Terminal section; Codex updates update only the Codex section. Report state is local and ignored by Git.

Discovery searches provider-name variants, cheaply filters and ranks search metadata, then fetches details only for a small configurable shortlist (`IG_DISCOVERY_DETAIL_BUDGET`, default 3) and classifies candidates as `CASH/ROLLING CFD`, `FUTURES/FORWARD`, `OTHER`, or `UNKNOWN`. Structural verification requires provider metadata to identify the requested normal weekday cash/rolling index; it does not require the current market status to be `TRADEABLE`. Weekend instruments, futures, options, ETFs, shares, knockouts, leveraged products, HSTECH, and China H-shares are excluded from primary verification. Hong Kong HS50 preserves distinct legitimate cash denominations as `VERIFIED_VARIANTS`. IG CFD labels do not represent official Nasdaq-100, Nikkei, or Hang Seng cash indexes. The streaming extra is optional:

```bash
uv pip install -e '.[streaming]'
```

The official Lightstreamer SDK boundary is covered with mocks. The SDK receives the `/session` `lightstreamerEndpoint`, the active account identifier, and `CST-...|XST-...` credentials; it creates MERGE `PRICE:{account}:{epic}` subscriptions with the `Pricing` adapter and required price fields. The SDK owns transport framing and reconnection. A real session uses the selected LIVE environment and runtime configuration; no live verification is implied by the test suite. `stream` authenticates through REST, resolves instruments through discovery, prefers the provider-confirmed $1 HS50 weekday cash variant when available, and never selects weekend HS50 or futures.

## Tests and security

```bash
pytest -q
ruff check .
```

Tests are unit/mock tests and never need IG credentials. Timestamps are timezone-aware and normalized to UTC. Candle aggregation supports 15M, 1H, 4H, and 1D, distinguishes forming from closed candles, and uses a configurable market timezone for daily boundaries. SQLite defaults to `data/ig_ai.sqlite3`, which is ignored by Git.

Never put API keys, passwords, CST, X-SECURITY-TOKEN, session tokens, raw market data, databases, or logs in source, tests, documentation, or commits. Logging includes a sensitive-field redaction filter.

Real IG verification status is recorded only from read-only runtime evidence. Run `ig-ai stream --duration 300` as the single production validation command: it discovers the three requested markets, uses one official Lightstreamer client, subscribes concurrently, and requires an independent subscription and ItemUpdate for US Tech 100, Japan 225, and Hong Kong HS50 before reporting PASS. The `PersistedStream` callback boundary queues SDK-thread updates and performs normalization, deduplication, SQLite writes, and candle updates on the database-owner thread; it reports received, normalized, persisted, and failed stages independently. Forming 15M, 1H, 4H, and 1D candles are restored from SQLite on restart, while shutdown preserves incomplete candles as open (`is_closed=0`). The official SDK owns reconnect/recovery; the report says `PASS`, `FAIL`, or `NOT OBSERVED` separately. `igai-report` records connection/subscription evidence separately from real price-update evidence and redacts session credentials. Candle counts demonstrate pipeline capability only; candle validation requires actual LIVE observations and is not claimed by the command itself.

## Phase 1 limitations and next phase

Phase 2A implements deterministic EMA, RSI, MACD, Bollinger Bands, ATR, candle geometry, objective gaps, confirmed/candidate swing primitives, rolling support/resistance inputs, synchronized divergence inputs, versioned SQLite feature upserts, and an outcome schema that stays empty until future candles exist. It does not implement candlestick libraries, chart-pattern lifecycles, direction scores, probabilities, alerts, news/macro regimes, backtesting, model calibration, automatic trading, or a production daemon supervisor. The automated suite does not claim LIVE streaming PASS: that status requires running `ig-ai stream --duration 300` against the real read-only LIVE account and reviewing the Terminal / Runtime Report. All current operations remain read-only.
