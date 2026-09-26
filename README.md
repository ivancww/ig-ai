# ig-ai

Technical-first market analysis infrastructure. Phase 1 provides a secure, read-only IG market-data foundation; it does not place trades or claim that IG CFD prices are official cash-index prices.

## Architecture

`config` validates environment configuration, `rest` handles IG REST sessions and read-only market endpoints, `discovery` searches rather than guesses EPICs, `streaming` provides a reconnect/resubscribe boundary, `normalization` converts provider updates into UTC observations, `candles` aggregates forming and closed OHLC candles, and `database` stores instruments, observations, and candles in SQLite. Technical features, signals, outcomes, and regimes are intentionally future schema extensions.

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
export IG_ACCOUNT_TYPE='DEMO'
```

The CLI validates credentials without printing them. `IG_ACCOUNT_TYPE` supports `DEMO` and `LIVE`; this application remains read-only in either mode. Optional settings include `IG_DATABASE_PATH`, `IG_REQUEST_TIMEOUT_SECONDS`, `IG_STREAM_RECONNECT_SECONDS`, and `IG_MARKET_TIMEZONE`.

## Commands

```bash
ig-ai db-init
ig-ai rest-check       # read-only authentication check
ig-ai discover         # searches US Tech 100, Japan 225, Hong Kong HS50
```

Discovery outputs candidate EPICs and metadata returned by IG. No EPIC is hardcoded or treated as verified until checked by the user/runtime. The streaming extra is optional:

```bash
uv pip install -e '.[streaming]'
```

The transport boundary is covered with fakes. A real Lightstreamer session requires secure Demo credentials and runtime configuration; no live verification is implied by the test suite.

## Tests and security

```bash
pytest -q
ruff check .
```

Tests are unit/mock tests and never need IG credentials. Timestamps are timezone-aware and normalized to UTC. Candle aggregation supports 15M, 1H, 4H, and 1D, distinguishes forming from closed candles, and uses a configurable market timezone for daily boundaries. SQLite defaults to `data/ig_ai.sqlite3`, which is ignored by Git.

Never put API keys, passwords, CST, X-SECURITY-TOKEN, session tokens, raw market data, databases, or logs in source, tests, documentation, or commits. Logging includes a sensitive-field redaction filter.

Real IG Demo verification status: **NOT VERIFIED** unless the user runs the read-only commands with secure environment credentials. The `Runtime` boundary installs SIGINT/SIGTERM handlers and delegates reconnect behavior to the streaming service; schedule and timezone remain configurable rather than hardcoded.

## Phase 1 limitations and next phase

This phase does not implement technical indicators, direction scores, probabilities, alerts, news/macro regimes, backtesting, model calibration, automatic trading, or a production daemon supervisor. Real Lightstreamer field decoding and real IG connectivity still require Demo verification. The recommended Phase 2 is to verify Demo discovery/streaming, persist configured instruments, harden provider-specific streaming parsing, and add feature computation over only closed candles before introducing signal/outcome records.
