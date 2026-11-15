# Lesson 9 progress: AI Trading Decision Engine (SIMULATION ONLY)

Last updated: 2026-11-15 21:10 KST (session 2). **Status: Lesson 9 COMPLETE. 117 tests passed, 0 failed.**
Read this file first when resuming. Do not re-run imports or rewrite existing modules.

## Repository state

- NOT a git repository: no branch / commit / diff. Track changes by file time.
- Do not touch the AQE project. Never print `.env`. No real orders, no broker/wallet, no xAI calls (xAI returns HTTP 403 billing).
- `data/` untouched in this session (paper_account.json 19:46, market_data.db 20:34). `data/decisions.db` is created on the
  first real "Run decision cycle" / `GET /api/decisions`.

## Existing implementation (verified)

| Area | Where | State |
|---|---|---|
| 6 agents + old cycle | `app/engine.py` | Random-walk simulator, own JSON account. UNCHANGED, still auto-fills (separate path) |
| Stocks (L6) | `app/stockdata.py` | `bars`: **0 real rows** -> stock proposals are NO TRADE on real data |
| Crypto (L7) | `app/cryptodata.py` | Kraken spot BTC/ETH/SOL; 60 m data up to the 2026-11-15 10:00Z close (DELAYED vs now) |
| Prediction markets (L8) | — | NOT implemented with real data -> always NO TRADE / RISK_REJECTED (unsupported) |
| Old risk bugs | `app/broker.py` day_start_equity never resets; `engine.decide()` no risk re-check | Still in the OLD simulator (left unchanged). The NEW path fixes both for itself |

## Files changed in Lesson 9

- New: `app/decisions.py`, `tests/test_decisions.py`.
- Modified: `app/main.py` (4 endpoints), `app/us_calendar.py` (`session_open`), `tests/conftest.py` (temp `decisions.DB_PATH`),
  `dashboard/index.html` (Decision Center view), `README.md`, this file.
- Unchanged: engine, broker, all 6 agent files, stockdata, cryptodata, config, `.env`, `data/*`.

## Design (as built)

1. `decisions.run()`: Market Analyst (`stockdata.detail` / `cryptodata.detail` 60 m -> `market_report`) -> News Analyst
   (never Grok; `NEWS_UNAVAILABLE`) -> `strategy_agent.run` (unchanged thresholds) -> `_risk` -> proposal row.
2. `_risk`: supported market, order type, no short, abnormal price (> 40 % jump), data freshness (`delayed`), market open
   (US session / Kraken pair online), duplicate waiting proposal, emergency stop, daily-loss state reliable (fail closed),
   `risk_manager.review` (daily loss, drawdown, 5 positions, 15 % size, 95 % cash, 30 % crypto), size at approval,
   cash incl. fee, max order qty, total exposure (5 x 15 % = 75 %). -> RISK_APPROVED / RISK_REJECTED / NEEDS_REVIEW.
3. `approve()`: fresh market data read, then `BEGIN IMMEDIATE`: expiry, content hash, data timestamp/price unchanged,
   full `_risk` again, then `_fill` (cash, position, fill row with before/after) in one transaction; UNIQUE(proposal_id).
4. `_performance`: `performance_analyst.run` on fills; counts of approved / risk-rejected / human-rejected / re-check
   blocked / expired / NO TRADE / HOLD; "N/A" where not computable.

## Tests (actual runs)

- Start of Lesson 9: 94 passed. After backend: 117 passed. Final: **117 passed, 0 failed** (23 in `test_decisions.py`).
- Real-data smoke (scratch ledger): 10 proposals in 0.5 s, all NO TRADE (stocks no data; BTC -0.17, ETH -0.26,
  SOL -0.47; crypto DELAYED; PM unsupported), 0 Grok requests.
- Browser (isolated server :8011, scratch ledger + paper copy, BTC as labeled TEST FIXTURE): run -> 1 BUY waiting;
  double-click Approve -> 1 fill (fee $15, cash 100,000 -> 84,985); API re-approve -> 409; approve during
  Emergency stop -> "Failed (HTTP 409) ... simulation running"; 390 px width: no page-level horizontal scroll.

## Offline verification (session 3, 2026-11-15)

Script outside the project (scratchpad `verify_l9.py`): real `app/decisions.py` code, fixture Kraken candles loaded into a
throwaway market DB through `cryptodata.fetch_kraken` with a local fake responder, one scratch ledger per scenario,
every non-Kraken urlopen blocked. Results: A PASS, B PASS (total-exposure cap at creation; size re-check at approval),
C PASS (record survives a new process), duplicate PASS (5 parallel approvals -> 1 fill), daily loss PASS
(same-day -9.1 % rejected, new-day re-base approved, stale price fail-closed), expired PASS. 0 internet calls, 0 Grok requests.
Wording fixes only: approve message for NOT_SENT / NOT_REQUIRED, "max order quantity" detail in money. pytest 117 passed.

## Known limits / open items

- Stock fills use the last completed daily close (no intraday stock data); only allowed during the US session.
- `session_open`: early-close days count as full days; DST switch taken at midnight UTC (shortcut comment in code).
- Total exposure cap 75 % is derived (no separate setting), so the "95 % of cash" limit rarely binds.
- Old simulator keeps its two known bugs (separate account, not used by the decision path).
- Real stock data still not fetched (Lesson 6 open item) -> stocks always NO TRADE until `python -m app.stockdata fetch`.

## Resume

```
.venv\Scripts\python.exe -m pytest -q          # expect 117 passed
.venv\Scripts\python.exe -m pytest -q -s tests/test_decisions.py -k end_to_end   # prints each step's input/output
.venv\Scripts\uvicorn.exe app.main:app --reload # tab "Decision Center (L9)"
```
