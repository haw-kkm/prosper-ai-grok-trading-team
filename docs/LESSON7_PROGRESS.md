# Lesson 7 progress: real crypto data (Kraken spot BTC/USD, ETH/USD, SOL/USD)

Read this first when resuming. Last updated: 2026-11-15 19:50 KST. **Lesson 7 is complete.** Real Kraken fetch verified
for BTC/USD 60 min only; ETH/USD, SOL/USD and all 5-min data are implemented and mock-tested but not fetched yet.

## Repository state

- Not a git repository: no branch / commit. Track by file time. Do not touch the AQE project.
- Lesson 6 open item still open: real Alpha Vantage stock fetch never run (stock `bars` table has 0 rows).
- Grok: key set, `.env` `XAI_MODEL=grok-4` (not on xAI's list), auto research 0. xAI account returns HTTP 403 billing. No Grok calls were made.

## Done

| Item | Where |
|---|---|
| Kraken client: AssetPairs mapping (cached 7 d, `crypto_pairs`), OHLC 60/5 min, open candle dropped, no retries, 30 s timeout, skip when up to date | `app/cryptodata.py` |
| Storage in `data/market_data.db`: `crypto_candles` (PK exchange, market_type SPOT/FUTURES, pair, interval, open_time UTC), `crypto_fetch_log` (coverage windows), `crypto_rejected` (abnormal raw rows + reason). Never overwrite; 1 transaction per batch | `app/cryptodata.py` |
| Gap classes (rejected / no candle from Kraken / never collected), zero-trade candles, delay vs last closed candle | `cryptodata.quality` |
| Kraken OHLCVT / header CSV import (labeled CSV, unverified) | `cryptodata.import_csv`, CLI |
| EMA 12/26 (`indicators.ema`), `market_analyst.market_report()` for stock + crypto (market, exchange, interval, analysis time, last close, quality, source, delay, limitations); `research_stocks` renamed `research_real` (not wired) | `app/indicators.py`, `app/agents/market_analyst.py` |
| Shared validation helpers `number()`, `ohlc_problem()`; stock detail uses `market_report` | `app/stockdata.py` |
| API `GET /api/crypto`, `GET /api/crypto/{BTC-USD}?interval=60|5` (read-only) | `app/main.py` |
| Dashboard "Crypto (Real)": pair + interval tabs, chart (close, SMA20, EMA12) with hover, volume bars, 10 KPIs, Market Analyst report, OHLCV table, fetch log, DELAYED / UP TO DATE, GROK OFFLINE, DATA UNAVAILABLE. Sim tab renamed "Crypto (simulated)" | `dashboard/index.html` |
| Tests `tests/test_cryptodata.py` (23) + host allowlist / no-network-at-start extended | `tests/` |
| README section | `README.md` |

## Real API results (2026-11-15)

1. 19:41:22 KST, `fetch BTC/USD --interval 60`: AssetPairs + OHLC (2 requests) reached Kraken, **status ERROR**
   (`bad_response`): my code looked for candles under "BTC/USD" (AssetPairs key when queried by display name) while
   OHLC answers under `XXBTZUSD`. Nothing stored. Fixed in `fetch_kraken` + regression test.
2. 19:42:56 KST, after the fix (pair map cached, 1 OHLC request): **OK**. Received 721, left out 1 open candle,
   stored 720, duplicates 0, rejected 0, gaps 0. Open times 2026-10-16 10:00Z .. 2026-11-15 09:00Z
   (last confirmed close 10:00Z). Indicators all computed (close 65,310.7, RSI 49.34, MACD -613.22, trend down -0.172).
3. Dashboard on port 8001 with the real DB: BTC/USD 60m shown; ETH/USD and 5-min show DATA UNAVAILABLE; 0 Grok requests.

## Tests

- 87 passed, 0 failed (test_api 3, test_cryptodata 23, test_engine 10, test_grok 23, test_stockdata 28). `node --check` OK.

## Known issues

- Two risk bugs from Lesson 3/4 still open (daily-loss baseline, approval skips Risk Manager re-check). Risk logic frozen.
- Dashboard browser checks from PowerShell 5.1: quote CSS selectors with single quotes (`"button[data-crypto='ETH/USD']"`);
  PS 5.1 strips inner double quotes.
- 720-candle cap: 60 min = 30 days, 5 min = 2.5 days. Longer history needs Kraken OHLCVT CSV import.
- Kraken rate limits are not documented on the OHLC page; the CLI pauses 2 s between pairs.

## Next commands

```
.venv\Scripts\python.exe -m pytest -q                                   # expect 87 passed
.venv\Scripts\python.exe -m app.cryptodata status
.venv\Scripts\python.exe -m app.cryptodata fetch --interval 60           # ETH/USD + SOL/USD (BTC skips if up to date)
.venv\Scripts\python.exe -m app.cryptodata fetch --interval 5
.venv\Scripts\python.exe -m app.stockdata fetch SPY                      # Lesson 6 open item, needs user OK
.venv\Scripts\uvicorn.exe app.main:app --reload                          # tabs "Crypto (Real)", "Stocks (Real)"
```

## Next priorities (Lesson 8)

1. Collect enough history (Kraken OHLCVT CSV for crypto, CSV/Alpha Vantage for stocks) before any model training.
2. Features from stored candles only up to each time (no look-ahead); time-ordered train/validation split.
3. Keep model output research-only until backtests exist; decide on fixing the two risk bugs before order simulation.
