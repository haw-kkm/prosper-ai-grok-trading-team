# Lesson 10 progress: AI Trading Risk Management System (SIMULATION ONLY)

Last updated: 2026-11-15 21:45 KST. **Status: Lesson 10 COMPLETE. pytest 151 passed, 0 failed.**
Read this file first when resuming. Lesson 9 notes: `docs/LESSON9_PROGRESS.md`.

## Repository state

- NOT a git repository: no branch / commit / diff. Track by file time.
- `data/` untouched (market_data.db 20:34, paper_account.json 19:46). `data/decisions.db` still does not exist; it is created
  (and upgraded in place on later versions) on the first decision cycle or `GET /api/decisions`.
- Never print `.env`. No real orders, no xAI calls, no data deletion.

## Before Lesson 10 (analysis)

| Feature | Before |
|---|---|
| Position size | 15 % for a NEW BUY only; held + new not summed; no bad-qty check |
| Exposure | crypto 30 % at cost, total 75 % (reject only), no currency check |
| Per-trade risk | missing (no stop price on proposals) |
| Daily loss | L9 UTC baseline, fail closed on stale prices; no corruption check; old simulator bug remains |
| Stop-loss | missing in the decision ledger |
| Cooldown | missing |
| Drawdown | dynamic halt in `review()` that lifts by itself, no record, no reset procedure |
| Approval | no block when cash / positions changed |
| Risk DB / dashboard | none |

## Done

| Item | Where |
|---|---|
| New settings (defaults; existing limits unchanged): MAX_TOTAL_EXPOSURE_PCT = 5 x 15 % = 75 %, RISK_PER_TRADE_PCT 1 %, SLIPPAGE_PCT 0.1 %, MAX_CONSECUTIVE_LOSSES 3, COOLDOWN_HOURS 24 | `app/config.py`, `.env.example` |
| Additive DB migration (`NEW_COLUMNS`), tables `risk_state`, `risk_events`, `risk_settings` | `app/decisions.py` |
| Sizing: position (held + new), STOCK / CRYPTO_SPOT / PREDICTION(0, UNSUPPORTED) exposure, total exposure, per-trade risk incl. fees + slippage, cash, existing `review()`; strictest wins; resize recorded; < 50 USD / < 1 share rejected; currency must be USD; bad qty rejected | `_limits`, `_risk`, `_create` |
| Stop -5 % / take-profit +10 % on every BUY proposal (in the content hash) and stored on the position | `_create`, `_fill` |
| Daily loss: UTC day, equity-based (realized + unrealized, fees), new-day re-base event, unreadable state -> fail closed | `_daily` |
| Simulated stop-loss: gap -> open, same candle -> stop first (AMBIGUOUS), bad candle skipped, data gaps / delay logged once, exit = BRACKET SELL of exactly the held qty, unique id per position | `check_stops`, `_evaluate`, `_exit`, `candles_since` |
| Cooldown on N closed losses (realized P&L incl. both fees), start/end events, BUY blocked, exits allowed | `_fill`, `_streak`, `_daily` |
| Kill switch latched in DB (time, reason, equity, peak, cash, positions), survives new day / restart / recovery; reviewed server-side reset only | `_daily`, `reset_kill_switch` |
| Approval blocked if cash / positions changed since the proposal | `_snapshot`, `approve` |
| API `POST /api/risk/check-stops`, `POST /api/risk/kill-switch/reset` | `app/main.py` |
| Dashboard "Risk Control (L10)" | `dashboard/index.html` |
| Tests A-W (34) | `tests/test_risk.py`; L9 test helper updated (named columns, CRYPTO_SPOT) |
| README section | `README.md` |

## Tests (actual runs)

- Before: 117 passed. Final: **151 passed, 0 failed** (api 3, cryptodata 24, decisions 23, engine 10, features 6, grok 23,
  risk 34, stockdata 28). `node --check` on dashboard JS OK.
- Scenario script (scratchpad `verify_l10.py`, fixture data, scratch ledgers): 1 normal, 2 position size, 3 total exposure,
  4 losing streak, 5 drawdown, 6 abnormal data: **all PASS**, 0 network attempts, 0 Grok requests.
- Real-data smoke (scratch ledger): 10 proposals, all NO TRADE (stocks no data, crypto weak + DELAYED, PM unsupported), 0.6 s.
- Browser (isolated :8012, fixture ledger): kill switch / cooldown banners, KPIs, stop events, rejected trades, risk log,
  pending SELL with Approve; reset refused with wrong phrase, accepted with review (history recorded); 390 px no overflow.

## Bugs found and fixed in Lesson 10

- NaN price crashed the size calculation -> now rejected (valid quantity / abnormal price).
- Risk Control view overflowed at 390 px (kill-switch history table outside a scroll box) -> fixed.

## Open items / risks

- Old random-walk simulator (`app/engine.py`, `app/broker.py`) still has its two old bugs (daily baseline never resets,
  approval without risk re-check). Separate account and fake prices; verified it never writes to `decisions.db`.
- Per-trade risk 1 % does not bind at defaults (15 % x 5.4 % = 0.81 %); the 95 % cash rule rarely binds under the 75 % cap.
- A held position without a fresh price blocks all new BUYs (fail closed). With delayed stored crypto data this is common.
- Daily baseline = first reliable valuation of the UTC day (not necessarily 00:00 exactly).
- Stops are evaluated when a cycle / the button runs (no timer), but always on the candle where they were hit.
- Kraken minimum order sizes are not verified (50 USD + 8 decimals used). No user accounts: anyone with dashboard access can
  submit a reset review (local app).
- Real stock data still not fetched (Lesson 6 open item).

## Resume

```
.venv\Scripts\python.exe -m pytest -q                      # expect 151 passed
.venv\Scripts\uvicorn.exe app.main:app --reload            # tabs "Decision Center (L9)", "Risk Control (L10)"
```
