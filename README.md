# Prosper AI: Grok AI Trading Team

A team of 6 AI agents that analyzes stocks, crypto and prediction markets, proposes trades, runs every proposal past a risk manager (and you, if you want), executes on a **simulated** account, and grades its own performance. All of it shows on a black-and-gold dashboard on your computer.

> **Simulation only.** Offline demo data, no brokerage connection, no real money. There is no code in this project that can place a real trade. Educational, not financial advice.

```
MARKET DATA (offline demo)
   ↓
MARKET ANALYST + NEWS ANALYST
   ↓
STRATEGY AGENT           → BUY / SELL / HOLD / NO TRADE
   ↓
RISK MANAGER             → APPROVED / REJECTED / NEEDS REVIEW
   ↓
HUMAN APPROVAL           → you click Approve or Reject (toggle on the dashboard)
   ↓
PORTFOLIO MANAGER        → simulated execution
   ↓
PERFORMANCE ANALYST      → return, win rate, drawdown, profit factor, fees, vs buy & hold
   ↓
TRADING DASHBOARD
```

---

## Setup (Lesson 2)

You need **Python 3.9 or newer** (https://www.python.org/downloads/; on Windows tick **"Add Python to PATH"**).

**1. Open a terminal in this folder.** If you put it on your Desktop:

```
cd ~/Desktop/prosper-ai-grok-trading-team
```

**2. Create a virtual environment and install the packages.**

Mac:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**3. Launch the dashboard:**

```
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser. The agents start working right away.

**4. Run the tests** in a second terminal (activate the virtual environment again first):

```
python -m pytest -v
```

You should see **87 passed**. The tests never call the real xAI, Alpha Vantage or Kraken APIs.

**Stop the app** any time with **Ctrl + C**. Your simulated account is saved in `data/`.

---

## Using the dashboard

- **Agent command center**: what each of the 6 agents is doing right now.
- **Trade decision center**: proposals waiting for you. Click **Approve** or **Reject**.
- **Human approval: ON/OFF**: when ON, every BUY waits for you. When OFF, trades the Risk Manager approves fill automatically. Weaker setups marked **NEEDS REVIEW** always wait for you.
- **Risk control center**: every verdict from the Risk Manager, including rejected trades and why.
- **Pause trading**: stops new trades. Stop-loss exits keep protecting open positions.
- **Reset account**: back to the starting simulated cash.

---

## Turn on Grok (optional, Lesson 5)

You don't need this to run the project. Without a key the dashboard shows **GROK: OFFLINE DEMO** and the team trades on market data alone.

1. Get an API key at https://console.x.ai (it costs money).
2. Copy `.env.example` and rename the copy to `.env`.
3. Paste your key after `XAI_API_KEY=`. `XAI_MODEL` picks the model (default `grok-4.3`).
4. Restart the app. The dashboard shows **GROK: NOT VERIFIED**.
5. Open the **Agents** tab and press **Test connection**. It sends one request that checks your key and model without generating text. You'll see **GROK: CONNECTED**, or **GROK: ERROR** with the reason (bad key, rate limit, network...).
6. Press **Run Grok research** to have Grok read the current simulated indicators, plus any news you provided.

What Grok can and can't do:

- It returns research only. It cannot place, size or approve orders, change risk limits, or skip the Risk Manager or your approval.
- The Market Analyst's Grok research is shown on the dashboard but is **not** used for trades.
- This project has no news feed. To try the News Analyst, create `data/news.json` with items like `[{"symbol": "AAPL", "headline": "...", "source": "...", "published": "2026-11-15"}]` (max 30). Grok scores only those items, every score must cite one, and scores stop counting after 15 minutes.
- Grok is never called on its own unless you set `GROK_AUTO_RESEARCH_SECONDS` (minimum 300). Failed requests are not retried.
- What was sent and what came back is logged in `data/grok_research.jsonl` (never your key).

If the key is wrong or Grok is down, the dashboard shows the error and the team keeps running on its rules.

**Never paste your API key in the community, a screenshot, or a chat.**

---

## Real US stock data (optional, Lesson 6)

The **Stocks (Real)** tab shows real daily history for SPY, QQQ, AAPL, NVDA and TSLA: price chart with SMA 5 / SMA 20, OHLCV, momentum, volatility, volume change, RSI 14, MACD, the data source and data warnings. Without stored data it says **DATA UNAVAILABLE**. It never shows simulated prices, and this data never feeds the trading simulation or any order.

Nothing is downloaded automatically. You run the commands (with the virtual environment active):

```
python -m app.stockdata status                 # what is stored
python -m app.stockdata fetch SPY              # 1 Alpha Vantage request (needs ALPHAVANTAGE_API_KEY in .env)
python -m app.stockdata fetch                  # all 5 symbols: 5 requests, 12 s apart
python -m app.stockdata import-csv spy.csv --symbol SPY --provider "Your source"
```

- Alpha Vantage free tier: `TIME_SERIES_DAILY`, `outputsize=compact` (latest 100 sessions), raw prices, 25 requests per day. The tool counts its own requests, refuses past 25 a day, and skips symbols that are already up to date. Paid endpoints are never called.
- CSV files need `Date, Open, High, Low, Close, Volume` columns (`Symbol`, `Provider`, `DataType` optional). Without a provider the data is labeled **source UNKNOWN, not verified**.
- Data is stored in `data/market_data.db` (SQLite). Bad rows are rejected (missing values, prices <= 0, high/low that don't contain open/close, negative volume, weekend or future dates); today's bar is skipped until the US session has closed. Stored bars are never overwritten: identical re-imports count as "same", different values for an existing day are reported as conflicts and the original is kept.

## Real crypto data (optional, Lesson 7)

The **Crypto (Real)** tab shows HISTORICAL Kraken spot candles for BTC/USD, ETH/USD and SOL/USD (60-min or 5-min): price chart with SMA 20 / EMA 12, volume bars, SMA 5/20, EMA 12/26, RSI 14, MACD, momentum, volatility, volume change, the Market Analyst report, data gaps and delay. It never shows simulated prices and never feeds the trading simulation.

```
python -m app.cryptodata status
python -m app.cryptodata fetch BTC/USD --interval 60     # Kraken public API, no key: 1 request (+1 AssetPairs if the pair map is older than 7 days)
python -m app.cryptodata fetch --interval 5              # all 3 pairs, 2 s apart
python -m app.cryptodata import-csv XBTUSD_60.csv --pair BTC/USD --interval 60   # Kraken's OHLCVT download or your CSV
```

- Kraken returns at most the **720 most recent candles** per interval; older history needs Kraken's official OHLCVT download or another legal CSV. The still-open candle is never stored.
- Pair names are checked with Kraken's AssetPairs (BTC/USD is `XBTUSD` / `XXBTZUSD` there). A pair Kraken doesn't list is reported, never invented.
- Stored in `data/market_data.db`, table `crypto_candles` (keyed by exchange, market type SPOT/FUTURES, pair, interval, candle start in UTC), separate from stocks. Abnormal candles are rejected and recorded in `crypto_rejected`. Missing candles are labeled, never filled in: *rejected*, *no candle from Kraken* (inside a window Kraken answered: likely no trades), or *never collected*.

## Trade Decision Center (Lesson 9, SIMULATION ONLY)

The **Decision Center (L9)** tab runs the six agents on your stored REAL data (stocks + Kraken spot) and asks you before anything is "filled". Press **Run decision cycle**: each symbol gets one proposal (BUY / SELL / HOLD / NO TRADE) with the market analysis, news status, strategy rule, every risk check and its result.

- Only BUY/SELL proposals that pass the Risk Manager wait for you, for 5 minutes. **Approve (paper)** re-runs every check (expiry, unchanged content, no newer market data, all risk limits) and then writes ONE simulated fill. A double click never fills twice. Nothing is sent to any exchange, broker or wallet.
- Separate paper ledger in `data/decisions.db` (proposals, positions, fills). The random-walk simulator and `data/paper_account.json` are not touched. Survives restarts.
- Data is HISTORICAL (latest stored closed bar). Delayed data, a closed US market, prediction markets (no real data yet), shorts, duplicates, the emergency stop or an unknown daily-loss state all block the order. With no stock data and a few hours' delay on crypto, most cycles end in NO TRADE or RISK_REJECTED. That is expected.
- API: `GET /api/decisions`, `POST /api/decisions/run`, `POST /api/decisions/{id}/approve` (body `{"content_hash": "..."}`), `POST /api/decisions/{id}/reject`.

## Risk Control Center (Lesson 10, SIMULATION ONLY)

The **Risk Control (L10)** tab shows the risk state of the same paper ledger. All rules are deterministic code; the strictest limit wins.

- **Size limits:** position (held + new) 15%, total exposure 75%, crypto spot 30%, prediction markets UNSUPPORTED, per-trade risk 1% of equity at the stop (5% below entry, incl. 0.1% fee + 0.1% slippage both ways), cash, 50 USD minimum, whole shares for stocks. A BUY larger than the limits is resized (both amounts recorded); with no room it is rejected. Non-USD prices are never converted.
- **Daily loss** (-3%): UTC day, baseline = first reliable equity of the day (realized + unrealized, after fees). Unknown prices or an unreadable state block new BUYs.
- **Simulated stop-loss / take-profit** on stored candles: gap below the stop fills at the open, stop and take-profit in one candle assumes the stop first, zero-volume or broken candles never fill, missing or delayed candles are logged. Exits were approved with the entry and never sell more than held. Button **Check stop-losses**, and at the start of each decision cycle.
- **Losing-streak cooldown:** 3 closed losing trades in a row (fees included) → no new BUYs for 24 h. Exits allowed.
- **Drawdown kill switch** (-10% from peak): latched in the database through new days and restarts. Exits allowed. Reset only via the form (`POST /api/risk/kill-switch/reset`): reviewer, note, the exact phrase `RESET KILL SWITCH`, the current trigger time and fresh prices. The peak is then re-based to current equity.
- An approval is blocked if paper cash or positions changed after the proposal was made. Every rejection, fill, stop, cooldown, kill-switch event and settings change is kept in `risk_events` / `risk_settings` in `data/decisions.db`.
- New `.env` settings: `MAX_TOTAL_EXPOSURE_PCT`, `RISK_PER_TRADE_PCT`, `SLIPPAGE_PCT`, `MAX_CONSECUTIVE_LOSSES`, `COOLDOWN_HOURS` (see `.env.example`).

## Backtesting Lab (Lesson 11, SIMULATION / HISTORICAL BACKTEST)

The **Backtest Lab (L11)** tab (or `python -m app.backtest run --symbol BTC-USD --interval 60 --policy SIMULATED_APPROVAL`) replays stored candles through the same agents and Lesson 10 risk rules.

- Data: Kraken spot BTC/ETH/SOL at 1m-1d (read-only, streamed in chunks), US stock daily bars when stored, DEMO synthetic data only when chosen (always labeled DEMO). Prediction markets: UNSUPPORTED. Too few candles → INSUFFICIENT_DATA, gaps → INCOMPLETE.
- Train 70% / Test 30% by time; each segment starts with a fresh account; Test warm-up candles come from before Test (indicators only).
- No look-ahead: decide at a candle's close, fill at the NEXT open + slippage; higher timeframes (4h, 1d) only from closed candles; no news for past dates (NEWS_UNAVAILABLE).
- Approval policy: `PROPOSAL_ONLY` (default, no fills) or `SIMULATED_APPROVAL` (backtest only, never a human approval, never reaches the live ledger).
- Costs: `BACKTEST_COSTS` in `app/config.py` are labeled ASSUMPTIONS (not verified quotes); each run can override them.
- Each run is stored in `data/backtests/<run_id>/` (config, code version, data range, result), written atomically.

---

## The six agents

| # | Agent | File | Job |
|---|---|---|---|
| 1 | Market Analyst | `app/agents/market_analyst.py` | Trend, moving averages, volatility → trend score |
| 2 | News Analyst | `app/agents/news_analyst.py` | Grok scores news you provide in `data/news.json`; otherwise says news is unavailable |
| 3 | Strategy Agent | `app/agents/strategy_agent.py` | BUY / SELL / HOLD / NO TRADE with reasoning |
| 4 | Risk Manager | `app/agents/risk_manager.py` | APPROVED / REJECTED / NEEDS REVIEW, sizing, stop-loss and take-profit |
| 5 | Portfolio Manager | `app/agents/portfolio_manager.py` | Simulated fills, cash, positions, P&L |
| 6 | Performance Analyst | `app/agents/performance_analyst.py` | Return, win rate, drawdown, profit factor, fees, benchmark |

Other files: `app/engine.py` runs the team every cycle, `app/market.py` makes the demo data, `app/broker.py` is the simulated account, `app/main.py` is the web app, `dashboard/index.html` is the dashboard.

## The three markets

| Market | Symbols | Notes |
|---|---|---|
| Stocks & ETFs | SPY, QQQ, AAPL, NVDA, TSLA | Whole shares only |
| Crypto | BTC-USD, ETH-USD, SOL-USD | More volatile; capped at 30% of the account |
| Prediction markets | PM-FED-CUT-DEC, PM-BTC-100K-EOY | Price = implied probability (0.01–0.99); half-size positions |

## Risk rules (edit in `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | 0.15 | Max 15% of the account per position |
| `MAX_OPEN_POSITIONS` | 5 | Max positions open at once |
| `STOP_LOSS_PCT` | 0.05 | Exit if a position is down 5% |
| `TAKE_PROFIT_PCT` | 0.10 | Exit if a position is up 10% |
| `MAX_DAILY_LOSS_PCT` | 0.03 | Pause new trades after a 3% down day |
| `MAX_DRAWDOWN_PCT` | 0.10 | Pause new trades after a 10% drop from the peak |
| `MAX_CRYPTO_EXPOSURE_PCT` | 0.30 | Max 30% of the account in crypto |
| `REVIEW_BELOW_CONFIDENCE` | 0.55 | Weaker setups are marked NEEDS REVIEW |
| `FEE_PCT` | 0.001 | Simulated 0.1% cost per trade |
| `REQUIRE_APPROVAL` | false | Start with human approval ON |

Risk rules reduce risk. They don't eliminate it.

---

## Prompts to try in Claude Code

Open this folder in Claude Code (`claude` in the terminal), then paste one:

- `Explain how the 6 agents in this project hand off to each other, in plain English.`
- `Walk me through what happens in one cycle of app/engine.py, step by step.`
- `Make the Risk Manager stricter for prediction markets. Explain the change and run the tests.`
- `Add AMZN and DOGE-USD to the watchlist and check the dashboard shows them.`
- `Add a 7th agent that writes a one-paragraph daily summary. Keep it simulation-only and add a test.`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found` | Install Python from python.org. On Windows use `python`. |
| `uvicorn: command not found` | Activate the virtual environment first (`source .venv/bin/activate`), then retry. |
| PowerShell says scripts are disabled | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer Y, then activate again. |
| `Address already in use` | It's already running in another terminal. Close it, or run `uvicorn app.main:app --reload --port 8001`. |
| Dashboard says DISCONNECTED | The terminal running uvicorn was closed. Start it again. |
| No trades yet | Normal for the first minute or two. The team only acts on strong setups. |

Still stuck? Copy the error into Claude Code and ask it to help, or post a screenshot in **Questions & Help** in Prosper AI. Never include your `.env` file or API key.
