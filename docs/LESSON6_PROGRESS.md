# Lesson 6 progress: real US stock data (SPY, QQQ, AAPL, NVDA, TSLA)

Read this first when resuming. Last updated: 2026-11-15, end of session 2. **Lesson 6 implementation is complete;
the real Alpha Vantage connection has not been tested yet (waiting for the user).**

## Repository state

- Not a git repository (`git status` -> "not a git repository"): no branch or commit. Track changes by file time.
- Original course project (pristine copy): `C:\claude\prosper-ai-grok-trading-team`. An old server from that folder
  may still run on port 8000; it does NOT have Lessons 4-6.
- Do not touch the separate AQE project.
- `.env` contains `ALPHAVANTAGE_API_KEY` (value set; never print it) and `XAI_MODEL=grok-4` (not on xAI's current list).

## Status

| Item | Status | Where |
|---|---|---|
| A. Alpha Vantage adapter: TIME_SERIES_DAILY, outputsize=compact, RAW, 25/day guard, skip if up to date, 30 s timeout, no retries, key redacted | COMPLETE (mocked) | `app/stockdata.py` `fetch_alpha_vantage`, `parse_alpha_vantage` |
| B. `ALPHAVANTAGE_API_KEY` | COMPLETE | `app/config.py`, `.env.example` |
| C. CSV import (Date/Timestamp, OHLCV; optional Symbol/Provider/DataType; provider UNKNOWN if absent) | COMPLETE | `import_csv`, CLI `import-csv` |
| D. OHLCV validation | COMPLETE | `_parse`, `validate`, schema CHECKs |
| E. SQLite `data/market_data.db` (`bars`, `ingest_log`), never overwrite, 1 transaction per batch, WAL, quick_check before CLI writes | COMPLETE | `connect`, `_store`, `_ingest`, `main` |
| US market calendar (NYSE holidays, special closures, bar final after 22:00 UTC) | COMPLETE | `app/us_calendar.py` |
| F/G. SMA5, SMA20, momentum, volatility (shared with simulator), volume change | COMPLETE | `market_analyst._indicators`, `analyze_bars` |
| H. RSI14, MACD, SMA series, no look-ahead | COMPLETE | `app/indicators.py` |
| I. Market Analyst real-data analysis; Grok interface `research_stocks` (NOT wired, never called) | COMPLETE | `app/agents/market_analyst.py` |
| J. Read-only API `GET /api/stocks`, `GET /api/stocks/{symbol}` | COMPLETE | `app/main.py` |
| K/L. Dashboard **Stocks (Real)** tab: 5 symbols, chart close+SMA5+SMA20, OHLCV, indicators, source, dates, warnings, DATA UNAVAILABLE / INSUFFICIENT DATA | COMPLETE (browser-checked on an isolated copy with synthetic CSV) | `dashboard/index.html` |
| M. Tests | COMPLETE: 64 passed, 0 failed | `tests/` |
| Real Alpha Vantage request | NOT VERIFIED (not run on purpose) | see commands below |

## Files changed in Lesson 6

New: `app/stockdata.py`, `app/us_calendar.py`, `app/indicators.py`, `tests/test_stockdata.py`, `docs/LESSON6_PROGRESS.md`.
Modified: `app/agents/market_analyst.py` (shared `_indicators`, `analyze_bars`, `research_stocks`, `research(data_source, fields)`),
`app/config.py`, `app/main.py`, `tests/conftest.py`, `dashboard/index.html` (Stocks tab, shared `lineChart`, "Sim Markets"),
`.env.example`, `.gitignore`, `README.md`.
Untouched: `app/market.py` (SimulatedMarket), `app/broker.py`, `app/engine.py`, risk/strategy/portfolio agents, `data/paper_account.json`.

## Commands

```
.venv\Scripts\python.exe -m pytest -q                         # expect 64 passed
.venv\Scripts\python.exe -m app.stockdata status
.venv\Scripts\python.exe -m app.stockdata fetch SPY           # ONE real free request; only with the user's OK
.venv\Scripts\python.exe -m app.stockdata fetch               # all 5 symbols = 5 requests, 12 s apart
.venv\Scripts\python.exe -m app.stockdata import-csv FILE.csv --symbol SPY --provider "Source name"
.venv\Scripts\uvicorn.exe app.main:app --reload               # dashboard, tab "Stocks (Real)"
```

## Test results (actual runs)

- Session 2 start: 63 passed, 1 failed (`test_sma_momentum_volatility_and_volume_change_known_values`, test bug).
- After fixes: 64 passed in three orders (normal; stock tests first; stock tests alone = 28 passed).
- Final: 64 passed (test_api 3, test_engine 10, test_grok 23, test_stockdata 28). `node --check` on dashboard JS: OK.
- Browser check (isolated copy, no keys, synthetic CSV): SPY 60 bars OK, NVDA 10 bars -> INSUFFICIENT DATA,
  AAPL no data -> DATA UNAVAILABLE + commands, mobile 390 px OK, equity chart still OK, server log 0 errors.

## Known bugs / limits

- Daily-loss baseline never resets on a new day (`app/broker.py:23`, `:56`). Risk logic frozen: not fixed.
- Human approval does not re-run the Risk Manager (`app/engine.py` `decide`). Not fixed.
- xAI returns HTTP 403 (credits / spending limit). Do not call Grok. Grok 403 is labeled `auth` (should be `billing`).
- RAW prices are not split-adjusted (adjusted data is a paid Alpha Vantage endpoint); >40% gaps are flagged.
- Alpha Vantage's daily-limit reset time is undocumented; the tool counts per UTC day.
- US calendar special closures are a hand-kept list (`SPECIAL_CLOSURES`).

## Next priorities

1. With the user's OK: `fetch SPY` once, check the Stocks tab, then `fetch` the other 4 (uses 5 of 25 free requests).
2. Lesson 7 (ML): build features from `bars` only up to each date (no look-ahead), time-based train/test split.
3. Later: decide whether to fix the two risk bugs before any real-data order simulation (Lesson 9).
