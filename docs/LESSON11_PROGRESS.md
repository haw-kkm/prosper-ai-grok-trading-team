# Lesson 11 progress: Multi-Market Backtesting Lab (SIMULATION / HISTORICAL BACKTEST)

Last updated: 2026-11-15 22:15 KST. **Status: Lesson 11 COMPLETE. pytest 169 passed, 0 failed, 0 skipped.**
Earlier notes: `docs/LESSON9_PROGRESS.md`, `docs/LESSON10_PROGRESS.md`.

## Repository state

- NOT a git repository: no branch / commit / diff. Track by file time.
- `data/market_data.db` unchanged (20:34; `-shm` / `-wal` are SQLite's own reader files), `paper_account.json` unchanged
  (19:46), no `data/decisions.db`. New: `data/backtests/34f54f91bb2a/` (first real run). `.env` unchanged.

## Done

| Item | Where |
|---|---|
| Pure shared risk helpers (live behavior unchanged): `buy_limits`, `stop_exit` | `app/decisions.py` |
| Cost assumptions `BACKTEST_COSTS` (crypto 0.40% fee = Kraken Pro 2024 lowest taker tier, NOT verified; stock 0.10% placeholder; slippage 0.10%) | `app/config.py` |
| Backtest engine: read-only chunked SQL source, stock + DEMO sources, 70/30 time split, warm-up, per-segment isolated account + Lesson 10 state, next-open fills, stops, cooldown, kill switch, daily loss, HTF context (closed candles only), metrics, Buy & Hold, run storage + CLI | `app/backtest.py` (new) |
| API `GET /api/backtest/options`, `POST /api/backtest/run`, `GET /api/backtest/runs`, `GET /api/backtest/runs/{id}` | `app/main.py` |
| Dashboard "Backtest Lab (L11)": settings form, runs list, Train/Test table, equity vs B&H, drawdown, price + trade markers, trades, risk events, proposals; `lineChart` got optional `marks` | `dashboard/index.html` |
| Tests (18; cover the 25 listed cases) | `tests/test_backtest.py` (new) |
| README section | `README.md` |

## First real backtest (run `34f54f91bb2a`, 9.4 s)

Kraken BTC/USD 1h, 103,656 candles 2013-10-06 21:00Z .. 2026-11-15 09:00Z (CSV archive 102,936 + API 720), quality
INCOMPLETE (train 10,854 / test 391 missing candles; largest test gap 2026-10-01 .. 10-16 = archive end -> API start).
Split 2023-04-13 10:00Z. Fee 0.40 %, slippage 0.10 %, SIMULATED_APPROVAL, Lesson 10 limits.

| | Train 2013-10-06 .. 2023-04-13 | Test 2023-04-13 .. 2026-11-15 |
|---|---|---|
| Return | +4.02 % (104,023.54) | **-9.89 % (90,111.17)** |
| Trades / win rate / PF | 132 / 37.1 % / 1.07 | 73 / 15.1 % / 0.24 |
| Max drawdown | 10.19 % | 10.15 % |
| Fees / slippage | 16,985.73 / 3,463.54 | 8,379.20 / 2,078.92 |
| Buy & Hold return / max DD | +24,555 % / 95.6 % | +114.97 % / 53.8 % |
| Kill switch | 2015-01-15 12:00Z (no trading after) | 2023-09-25 21:00Z (no trading after) |

## Bugs found and fixed in Lesson 11

- Progress entry removed before the final result file was written -> a reader could see a false INTERRUPTED (fixed: write first).
- Dashboard equity input `step=1000, min=1` made 100000 invalid -> the browser silently blocked "Run" (fixed: `step="any"`).
- Phone-width overflow from the results table / long label (fixed).

## Open items

- Stocks: 0 stored bars -> INSUFFICIENT_DATA until `python -m app.stockdata fetch` / CSV import.
- Prediction markets: UNSUPPORTED. No point-in-time news archive (NEWS_UNAVAILABLE everywhere).
- One symbol per run (no multi-asset portfolio). Validation / walk-forward: structure ready (`_segment`), not exposed.
- Real fee schedule not verified; cost results depend on the assumptions.

## Resume

```
.venv\Scripts\python.exe -m pytest -q                                  # expect 169 passed
.venv\Scripts\python.exe -m app.backtest run --symbol BTC-USD --interval 60 --policy SIMULATED_APPROVAL
.venv\Scripts\uvicorn.exe app.main:app --reload                        # tab "Backtest Lab (L11)"
```
