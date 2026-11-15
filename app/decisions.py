"""Lesson 9 + 10: Trade Decision Engine with risk management. SIMULATION ONLY.

    Market Analyst (stored REAL data: Lesson 6 stocks, Lesson 7 Kraken spot) + News Analyst
    -> Strategy Agent -> Risk Manager -> human approval -> Portfolio Manager (paper ledger) -> Performance Analyst

- Everything lives in its own SQLite file, data/decisions.db: proposals, the paper ledger (account, positions), fills,
  risk state (kill switch, cooldown), risk events and risk-settings history. The old random-walk simulator
  (app/engine.py) and its data/paper_account.json are not touched.
- There is no order API here. "Executing" means writing a fill row to decisions.db. Market data is only READ.
- Nothing fills without approve(), and approve() re-runs every risk check first. HOLD / NO TRADE never create an order.
  The one exception is a bracket exit (stop-loss / take-profit), which the human approved together with the entry and
  which can only reduce a held position.
- Grok is never called. Strategy and risk are deterministic code; no AI output can size or approve an order.

Lesson 10 risk rules (all deterministic, the strictest limit wins):
  position size (held + new), market exposure (STOCK / CRYPTO_SPOT / PREDICTION=unsupported), total exposure,
  per-trade risk at the stop incl. fees + slippage, cash, daily loss (UTC day), losing-streak cooldown,
  drawdown kill switch (latched until a reviewed reset).
"""

import contextlib
import datetime as dt
import hashlib
import json
import math
import sqlite3
import uuid

from . import config, cryptodata, stockdata, us_calendar
from .agents import market_analyst, news_analyst, performance_analyst, portfolio_manager, risk_manager, strategy_agent
from .engine import PENDING_EXPIRES_SECONDS as EXPIRES_SECONDS  # same 5 minutes as the old approval queue
from .market import market_type

DB_PATH = config.DATA_DIR / "decisions.db"
CRYPTO_INTERVAL = 60             # minutes; the Crypto (Real) screen's default
ORDER_TYPE = "SIMULATED_MARKET"  # the only order type: paper fill at the reference price + FEE_PCT
ETFS = {"SPY", "QQQ"}
MARKET_CLASS = {"stock": "STOCK", "crypto": "CRYPTO_SPOT", "prediction": "PREDICTION"}
ACCOUNT_CURRENCY = "USD"         # no verified FX rates in this project: other currencies are never converted
MIN_ORDER_USD = 50               # same minimum as risk_manager.review ("budget < 50")
DAY_TIMEZONE = "UTC"             # the daily-loss day starts at 00:00 UTC
RESET_PHRASE = "RESET KILL SWITCH"
BRACKET = "PRE_APPROVED_WITH_ENTRY"  # approval status of a stop-loss / take-profit exit
DATA_MODE = "HISTORICAL (stored confirmed bars, not real-time)"
RESEARCH_SOURCE = "NONE: Grok offline, no xAI calls in the decision engine"
INDICATORS = ("price", "sma_fast", "sma_slow", "ema_12", "ema_26", "rsi_14", "macd", "momentum_pct", "volatility_pct",
              "volume_change_pct", "trend", "trend_score")
RULE = (f"score = trend score (SMA 5 vs SMA 20, 20-bar momentum vs volatility), 70/30 blended with the news score only "
        f"when news is available. BUY if score >= {strategy_agent.BUY_THRESHOLD} and not held; SELL the whole held "
        f"position if score <= {strategy_agent.SELL_THRESHOLD}; HOLD if held; otherwise NO TRADE. No short selling.")
HASHED = ("proposal_id", "symbol", "market_type", "instrument_type", "signal", "order_type", "proposed_quantity",
          "reference_price", "data_timestamp_utc", "expires_at", "stop_price", "take_profit_price")
JSON_FIELDS = ("reasoning", "market_analysis", "news_analysis", "risk_checks")
SETTINGS_KEYS = ("MAX_POSITION_PCT", "MAX_OPEN_POSITIONS", "MAX_TOTAL_EXPOSURE_PCT", "MAX_CRYPTO_EXPOSURE_PCT",
                 "RISK_PER_TRADE_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "SLIPPAGE_PCT", "FEE_PCT", "MAX_DAILY_LOSS_PCT",
                 "MAX_DRAWDOWN_PCT", "MAX_CONSECUTIVE_LOSSES", "COOLDOWN_HOURS", "REVIEW_BELOW_CONFIDENCE")

AWAITING, APPROVED, REJECTED, EXPIRED = "AWAITING_HUMAN_APPROVAL", "HUMAN_APPROVED", "HUMAN_REJECTED", "EXPIRED"
RISK_APPROVED, RISK_REJECTED, NEEDS_REVIEW = "RISK_APPROVED", "RISK_REJECTED", "NEEDS_REVIEW"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
    market_type TEXT NOT NULL, exchange_or_provider TEXT, symbol TEXT NOT NULL, instrument_type TEXT NOT NULL,
    signal TEXT NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD', 'NO TRADE')), order_type TEXT,
    proposed_quantity REAL, proposed_notional REAL, original_quantity REAL, original_notional REAL, size_adjustment TEXT,
    reference_price REAL, currency TEXT,
    analysis_time_utc TEXT NOT NULL, data_timestamp_utc TEXT, data_mode TEXT,
    reasoning TEXT NOT NULL, market_analysis TEXT, news_analysis TEXT,
    supporting_research TEXT, research_source TEXT NOT NULL, confidence REAL,
    risk_decision TEXT, risk_reason TEXT, risk_checks TEXT,
    human_approval_status TEXT NOT NULL, decided_at TEXT, execution_status TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT, content_hash TEXT
);
CREATE INDEX IF NOT EXISTS proposals_waiting ON proposals (human_approval_status, symbol);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1), starting_cash REAL NOT NULL, cash REAL NOT NULL CHECK (cash >= 0),
    peak_equity REAL NOT NULL, day_date TEXT NOT NULL, day_start_equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, market_type TEXT NOT NULL, qty REAL NOT NULL CHECK (qty > 0), avg_price REAL NOT NULL,
    opened_at TEXT NOT NULL, mark_price REAL, mark_time TEXT, mark_fresh INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals (proposal_id),   -- one fill per proposal, ever
    side TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL CHECK (qty > 0), price REAL NOT NULL CHECK (price > 0),
    notional REAL NOT NULL, fee REAL NOT NULL, realized_pnl REAL,
    cash_before REAL NOT NULL, cash_after REAL NOT NULL, position_before REAL NOT NULL, position_after REAL NOT NULL,
    equity_after REAL NOT NULL, approved_at TEXT NOT NULL, filled_at TEXT NOT NULL, agent TEXT NOT NULL, fill_model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch_active INTEGER NOT NULL DEFAULT 0, kill_triggered_at TEXT, kill_reason TEXT, kill_equity REAL, kill_peak REAL,
    cooldown_until TEXT, cooldown_started_at TEXT, cooldown_reason TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, kind TEXT NOT NULL,
    symbol TEXT, proposal_id TEXT, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, settings TEXT NOT NULL);
INSERT OR IGNORE INTO risk_state (id) VALUES (1);
"""
# Lesson 10 columns, added to Lesson 9 tables in place (new files go through the same path, so it is always exercised).
NEW_COLUMNS = {
    "positions": ("stop_price REAL", "take_profit_price REAL", "entry_fees REAL NOT NULL DEFAULT 0", "checked_until INTEGER",
                  "entry_proposal_id TEXT", "last_warning TEXT"),
    "proposals": ("stop_price REAL", "take_profit_price REAL", "account_snapshot TEXT"),
}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


def _ts(text):
    """'2026-11-16T15:00Z' or '2026-11-13' (midnight UTC) -> Unix seconds."""
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int((t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp())


def _utc(unix):
    return dt.datetime.fromtimestamp(unix, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _migrate(conn):
    """Additive only: adds missing Lesson 10 columns. Nothing is dropped or rewritten."""
    for table, cols in NEW_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                except sqlite3.OperationalError as err:  # another connection added it a moment ago
                    if "duplicate column" not in str(err):
                        raise


@contextlib.contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # autocommit; writes use explicit BEGIN IMMEDIATE
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
    finally:
        conn.close()  # an uncommitted transaction is rolled back: never a half-saved approval, fill or risk state


def _account(conn, now):
    conn.execute("INSERT OR IGNORE INTO account VALUES (1, ?, ?, ?, ?, ?)",
                 (config.STARTING_CASH, config.STARTING_CASH, config.STARTING_CASH, now.date().isoformat(), config.STARTING_CASH))
    return dict(conn.execute("SELECT * FROM account").fetchone())


def _risk_state(conn):
    return dict(conn.execute("SELECT * FROM risk_state").fetchone())


def _positions(conn):
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM positions")}


def _proposal(conn, pid):
    row = conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
    return dict(row) if row else None


def _event(conn, now, kind, symbol=None, proposal_id=None, **detail):
    conn.execute("INSERT INTO risk_events (time, kind, symbol, proposal_id, detail) VALUES (?, ?, ?, ?, ?)",
                 (_iso(now), kind, symbol, proposal_id, json.dumps(detail, default=str)))
    return {"time": _iso(now), "kind": kind, "symbol": symbol, "proposal_id": proposal_id, **detail}


def _insert(conn, table, row):
    row = {k: json.dumps(v, default=str) if k in JSON_FIELDS and not isinstance(v, str) else v for k, v in row.items()}
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _hash(p):
    values = [float(v) if isinstance(v, int) else v for v in (p.get(k) for k in HASHED)]  # SQLite REAL returns 3 as 3.0
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()[:16]


def _snapshot(conn):
    """Fingerprint of paper cash + positions. A proposal approved after either changed is blocked."""
    cash = conn.execute("SELECT cash FROM account").fetchone()[0]
    held = sorted((r["symbol"], round(r["qty"], 10)) for r in conn.execute("SELECT symbol, qty FROM positions"))
    return hashlib.sha256(json.dumps([round(cash, 6), held]).encode()).hexdigest()[:16]


def _unit(kind, qty):
    """Round DOWN to what the market trades: whole shares for stocks, 8 decimals (Kraken lot size) for crypto."""
    return math.floor(qty + 1e-9) if kind == "stock" else math.floor(qty * 1e8 + 1e-6) / 1e8


def _market_limits():
    """Exposure cap per market class. STOCK has no separate setting, so the total cap applies."""
    return {"STOCK": config.MAX_TOTAL_EXPOSURE_PCT, "CRYPTO_SPOT": config.MAX_CRYPTO_EXPOSURE_PCT, "PREDICTION": 0.0}


def _record_settings(conn, now):
    """Store the risk settings in force; every change (e.g. an edited .env) is kept as history."""
    new = {k: getattr(config, k) for k in SETTINGS_KEYS}
    last = conn.execute("SELECT settings FROM risk_settings ORDER BY id DESC LIMIT 1").fetchone()
    old = json.loads(last[0]) if last else None
    if new != old:
        conn.execute("INSERT INTO risk_settings (recorded_at, settings) VALUES (?, ?)", (_iso(now), json.dumps(new)))
        if old:
            _event(conn, now, "RISK_SETTINGS_CHANGED", changed={k: [old.get(k), v] for k, v in new.items() if old.get(k) != v})
        else:
            _event(conn, now, "RISK_SETTINGS_RECORDED", settings=new)


# ---------- Market Analyst + News Analyst ----------
def market_view(symbol, now):
    """Market Analyst report on stored REAL data (read-only). Stocks: Lesson 6 daily bars. Crypto: Lesson 7 Kraken spot."""
    kind = market_type(symbol)
    if kind == "prediction":
        return {"symbol": symbol, "type": kind, "enough_data": False, "status": "UNSUPPORTED", "bars": 0,
                "data_source": "none", "exchange": None, "warnings": [],
                "limitations": "Prediction markets have no verified real data (Lesson 8 not implemented). "
                               "Simulated PM-* prices are never used for decisions."}
    if kind == "stock":
        d, extra = stockdata.detail(symbol, now), {"currency": "USD", "trading_hours": "US regular session 09:30-16:00 New York time"}
        closes = [b["close"] for b in d["bars"]]
    else:
        d = cryptodata.detail(symbol.replace("-", "/"), CRYPTO_INTERVAL, now)
        extra = {"currency": d["quote"], "pair_status": d["pair_status"], "trading_hours": "24/7 (Kraken spot)"}
        closes = [c["close"] for c in d["candles"]]
    return {**d["analysis"], **extra, "symbol": symbol, "prev_close": closes[-2] if len(closes) > 1 else None,
            "warnings": d["warnings"], "data_mode": DATA_MODE}


def _news(views):
    """Existing News Analyst, never with Grok. Without a Grok score every symbol is NEWS_UNAVAILABLE (score 0)."""
    scores, note = news_analyst.run(views, news_items=news_analyst.load_news())
    return scores, {sym: {"status": "NEWS_SCORED" if s else "NEWS_UNAVAILABLE", "score": s, "note": note} for sym, s in scores.items()}


# ---------- paper ledger valuation + daily / drawdown / cooldown state ----------
def _equity(conn, now):
    """(cash + positions at their latest analyzed price, symbols whose price is not fresh)."""
    cash = conn.execute("SELECT cash FROM account").fetchone()[0]
    value, stale = cash, []
    for p in _positions(conn).values():
        value += p["qty"] * (p["mark_price"] or p["avg_price"])
        age = (now - dt.datetime.fromisoformat(p["mark_time"])).total_seconds() if p["mark_time"] else None
        if not p["mark_fresh"] or age is None or age > EXPIRES_SECONDS:
            stale.append(p["symbol"])
    return value, stale


def _streak(conn):
    """Closed losing trades in a row, newest first. Only SELL fills count (realized P&L incl. both fees)."""
    n = 0
    for (pnl,) in conn.execute("SELECT realized_pnl FROM fills WHERE side='SELL' ORDER BY fill_id DESC"):
        if pnl is None or pnl >= 0:
            break
        n += 1
    return n


def _daily(conn, now):
    """(day_start_equity, equity, problem). Updates the daily baseline, the peak, the kill switch and the cooldown.

    Day = 00:00-24:00 UTC. Daily P&L = equity now - equity at the first reliable valuation of the UTC day; equity =
    cash (after all fees) + open positions at the latest analyzed price, so realized AND unrealized P&L both count.
    Fail closed: `problem` is set when that can't be computed (stale prices) or the stored state is unreadable.
    The baseline is re-based on a new UTC day (the old PaperBroker never did: Lesson 3 bug, still in app/broker.py).
    The drawdown kill switch is latched in risk_state: a new day, a restart or a recovery never clears it.
    """
    acct, st = _account(conn, now), _risk_state(conn)
    if st["cooldown_until"] and now >= dt.datetime.fromisoformat(st["cooldown_until"]):
        _event(conn, now, "COOLDOWN_ENDED", started_at=st["cooldown_started_at"], until=st["cooldown_until"])
        conn.execute("UPDATE risk_state SET cooldown_until=NULL, cooldown_started_at=NULL, cooldown_reason=NULL WHERE id=1")
    equity, stale = _equity(conn, now)
    try:
        readable = acct["day_start_equity"] > 0 and acct["peak_equity"] > 0 and dt.date.fromisoformat(acct["day_date"]) <= now.date()
    except (TypeError, ValueError):
        readable = False
    if not readable:
        return None, equity, "daily-loss state is unreadable (baseline, peak or date invalid): new BUYs blocked until it is fixed"
    if stale:
        return None, equity, f"no fresh price for open position(s) {', '.join(stale)}, so equity and today's loss are unknown"
    today = now.date().isoformat()
    if acct["day_date"] != today:
        conn.execute("UPDATE account SET day_date=?, day_start_equity=? WHERE id=1", (today, equity))
        _event(conn, now, "DAILY_BASELINE", date=today, baseline=equity, previous_date=acct["day_date"],
               previous_baseline=acct["day_start_equity"])
        acct["day_start_equity"] = equity
    peak = max(acct["peak_equity"], equity)
    conn.execute("UPDATE account SET peak_equity=? WHERE id=1", (peak,))
    drawdown = (peak - equity) / peak
    if drawdown >= config.MAX_DRAWDOWN_PCT and not st["kill_switch_active"]:
        reason = f"drawdown {drawdown:.2%} from peak {peak:,.2f} reached the {config.MAX_DRAWDOWN_PCT:.0%} limit"
        conn.execute("UPDATE risk_state SET kill_switch_active=1, kill_triggered_at=?, kill_reason=?, kill_equity=?, kill_peak=? "
                     "WHERE id=1", (_iso(now), reason, equity, peak))
        _event(conn, now, "KILL_SWITCH_TRIGGERED", reason=reason, equity=equity, peak=peak, cash=acct["cash"],
               positions={s: p["qty"] for s, p in _positions(conn).items()})
    return acct["day_start_equity"], equity, None


def _mark(conn, views, now):
    """Store the latest analyzed price of held symbols, so equity uses real (stored) prices."""
    for v in views:
        fresh = bool(v.get("enough_data") and not v.get("delayed") and v.get("price"))
        conn.execute("UPDATE positions SET mark_price=COALESCE(?, mark_price), mark_time=?, mark_fresh=? WHERE symbol=?",
                     (v.get("price"), _iso(now), int(fresh), v["symbol"]))


# ---------- Risk Manager ----------
def _limits(conn, sym, price, stop, equity, cash, review_qty):
    """Lesson 10 limits for the live ledger: every position counted once, at its latest analyzed price; waiting
    (unfilled) proposals are not exposure, they are re-checked when approved."""
    value = {s: r["qty"] * (r["mark_price"] or r["avg_price"]) for s, r in _positions(conn).items()}
    return buy_limits(sym, price, stop, equity, cash, value, review_qty)


def buy_limits(sym, price, stop, equity, cash, value, review_qty, fee_pct=None, slippage_pct=None):
    """Largest BUY quantity (units) each limit allows, with how it was computed. The smallest one wins.
    Pure (no database): shared by the live engine and the backtest (app/backtest.py). `value` = {symbol: market value}."""
    cls = MARKET_CLASS[market_type(sym)]
    held = value.get(sym, 0.0)
    in_market = sum(v for s, v in value.items() if MARKET_CLASS[market_type(s)] == cls)
    total = sum(value.values())
    fee = config.FEE_PCT if fee_pct is None else fee_pct
    cap, worse = _market_limits()[cls], fee + (config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct)
    unit_risk = price * (1 + worse) - stop * (1 - worse)  # loss per unit if stopped out, incl. fees + slippage both ways
    budget = equity * config.RISK_PER_TRADE_PCT
    return {
        "position size": ((equity * config.MAX_POSITION_PCT - held) / price,
                          f"{config.MAX_POSITION_PCT:.0%} of equity = {equity * config.MAX_POSITION_PCT:,.2f}, already held {held:,.2f}"),
        f"{cls} exposure": ((equity * cap - in_market) / price, f"{cap:.0%} of equity = {equity * cap:,.2f}, already {in_market:,.2f}"),
        "total exposure": ((equity * config.MAX_TOTAL_EXPOSURE_PCT - total) / price,
                           f"{config.MAX_TOTAL_EXPOSURE_PCT:.0%} of equity = {equity * config.MAX_TOTAL_EXPOSURE_PCT:,.2f}, already {total:,.2f}"),
        "per-trade risk": (budget / unit_risk, f"{config.RISK_PER_TRADE_PCT:.2%} of equity = {budget:,.2f} max loss; "
                                               f"{unit_risk:,.4f} lost per unit at stop {stop:,.4f} incl. fees + slippage"),
        "cash": (cash / (price * (1 + worse)), f"paper cash {cash:,.2f}, incl. fee + slippage"),
        "Risk Manager rules": (review_qty, "existing review(): size 15%, 95% of cash, crypto 30% at cost, whole shares"),
    }


def _risk(conn, p, view, now, halted, at_approval=False):
    """Every risk check for one BUY/SELL proposal, deterministic. Returns (decision, reason, checks, allowed_qty).

    BUY: all limits are computed and the strictest wins (resized if smaller, rejected if below the minimum order).
    At approval nothing is resized: a proposal larger than what the limits allow now is rejected.
    SELL: allowed even during a kill switch / cooldown / daily-loss halt, but never more than held (no shorts).
    """
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    sym, sig, price, qty = p["symbol"], p["signal"], p["reference_price"], p["proposed_quantity"] or 0
    kind = market_type(sym)
    held = _positions(conn).get(sym)
    prev = view.get("prev_close")
    check("supported market", kind in ("stock", "crypto"), "US stock/ETF or Kraken spot crypto" if kind != "prediction"
          else "PREDICTION markets are UNSUPPORTED (no verified data, Lesson 8 not implemented)")
    check("supported order type", p.get("order_type") == ORDER_TYPE and sig in ("BUY", "SELL"),
          f"{p.get('order_type')} {sig} (only {ORDER_TYPE} BUY/SELL; no shorts, no leverage)")
    check("valid quantity", isinstance(qty, (int, float)) and math.isfinite(qty) and qty > 0 and (kind != "stock" or qty == int(qty)),
          f"{qty} ({'whole shares' if kind == 'stock' else 'up to 8 decimals'})")
    check("no short selling", sig != "SELL" or (held and 0 < qty <= held["qty"] + 1e-9),
          f"sell {qty} of held {held['qty'] if held else 0}" if sig == "SELL" else "BUY opens or adds to a long position")
    check("abnormal price", isinstance(price, (int, float)) and math.isfinite(price) and price > 0
          and (not prev or abs(price / prev - 1) <= stockdata.SPLIT_GAP),
          f"price {price}, previous close {prev}, max jump {stockdata.SPLIT_GAP:.0%}")
    check("currency", (view.get("currency") or p.get("currency")) == ACCOUNT_CURRENCY,
          f"{view.get('currency') or p.get('currency')}; account is {ACCOUNT_CURRENCY} and there is no verified FX rate to convert")
    check("data freshness", view.get("enough_data") and not view.get("delayed"),
          f"latest data {view.get('analysis_time')}" + (" is DELAYED: newer closed bars exist but are not stored"
                                                        if view.get("delayed") else ""))
    if kind == "crypto":
        check("market open", view.get("pair_status") == "online", f"Kraken spot 24/7, pair status {view.get('pair_status')}")
    elif kind == "stock":
        is_open = us_calendar.session_open(now)
        check("market open", is_open, f"US regular session is {'open' if is_open else 'CLOSED'} at {_iso(now)}")
    dup = conn.execute("SELECT proposal_id FROM proposals WHERE symbol=? AND human_approval_status=? AND proposal_id<>?",
                       (sym, AWAITING, p.get("proposal_id") or "")).fetchone()
    check("duplicate proposal", dup is None, f"proposal {dup[0]} for {sym} is already waiting" if dup else "none waiting")
    if sig == "BUY":
        stop = p.get("stop_price")
        check("stop-loss price", isinstance(stop, (int, float)) and math.isfinite(stop) and 0 < stop < (price or 0),
              f"stop {stop:,.4f}, entry {price:,.4f}, distance {(1 - stop / price):.2%}"
              if isinstance(stop, (int, float)) and price else "MISSING: the loss at the stop can't be computed, so no new entry")
        check("simulation running", not halted, "EMERGENCY STOP is on: no new BUYs" if halted else "running")
        day_start, equity, problem = _daily(conn, now)  # may latch the kill switch or end a cooldown first
        st = _risk_state(conn)
        check("daily loss state reliable", problem is None, problem or f"baseline {day_start:,.2f} for {now.date()} ({DAY_TIMEZONE})")
        check("drawdown kill switch", not st["kill_switch_active"],
              f"ACTIVE since {st['kill_triggered_at']}: {st['kill_reason']}. Only a reviewed reset clears it"
              if st["kill_switch_active"] else "not active")
        check("losing-streak cooldown", not st["cooldown_until"],
              f"ACTIVE until {st['cooldown_until']}: {st['cooldown_reason']}" if st["cooldown_until"]
              else f"not active ({_streak(conn)} loss(es) in a row, limit {config.MAX_CONSECUTIVE_LOSSES})")

    needs_review, allowed = False, qty
    if all(c["ok"] for c in checks):
        acct = _account(conn, now)
        positions = _positions(conn)
        if sig == "SELL":
            day_start, equity = acct["day_start_equity"], _equity(conn, now)[0]
        [v], _ = risk_manager.review([{"symbol": sym, "action": sig, "price": price, "confidence": p["confidence"]}],
                                     positions, equity, acct["cash"], day_start, acct["peak_equity"])
        check("Risk Manager rules", v["verdict"] != "REJECTED", v["risk_note"])
        if sig == "BUY" and v["verdict"] != "REJECTED":
            needs_review = v["verdict"] == "NEEDS REVIEW"
            limits = _limits(conn, sym, price, p["stop_price"], equity, acct["cash"], v["qty"])
            binding = min(limits, key=lambda k: limits[k][0])
            room = _unit(kind, limits[binding][0])
            for name, (cap, detail) in limits.items():
                check(f"limit: {name}", True, f"max {max(_unit(kind, cap), 0):g} units = {max(cap, 0) * price:,.2f} | {detail}"
                      + (" | BINDING" if name == binding and room < qty - 1e-9 else ""))
            if at_approval:
                check("size within current limits", qty <= room + 1e-9, f"approved {qty:g}, limits now allow {room:g} ({binding})")
            else:
                allowed = min(qty, room)
                check("minimum order", allowed >= (1 if kind == "stock" else 1e-8) and allowed * price >= MIN_ORDER_USD,
                      f"{allowed:g} units = {allowed * price:,.2f} after the strictest limit ({binding}); "
                      f"minimum {MIN_ORDER_USD} {ACCOUNT_CURRENCY}" + (" and 1 share" if kind == "stock" else ""))

    failed = [c for c in checks if not c["ok"]]
    if failed:
        return RISK_REJECTED, "; ".join(f"{c['check']}: {c['detail']}" for c in failed), checks, 0
    if needs_review:
        return (NEEDS_REVIEW, f"All {len(checks)} checks passed, but confidence {p['confidence']} is below "
                f"{config.REVIEW_BELOW_CONFIDENCE}: look closely before approving.", checks, allowed)
    return RISK_APPROVED, f"All {len(checks)} checks passed.", checks, allowed


# ---------- Strategy Agent -> proposal record ----------
def _create(conn, run_id, s, view, news, now, halted, equity):
    sym, sig = s["symbol"], s["action"]
    kind = market_type(sym)
    price = view.get("price")
    held = _positions(conn).get(sym)
    qty = stop = take = None
    if sig == "BUY":
        valid = isinstance(price, (int, float)) and math.isfinite(price) and price > 0  # a bad price is rejected below, not sized
        qty = _unit(kind, equity * config.MAX_POSITION_PCT / price) if valid else 0  # target size: code, never an AI number
        stop, take = price * (1 - config.STOP_LOSS_PCT), price * (1 + config.TAKE_PROFIT_PCT)
    elif sig == "SELL":
        qty = held["qty"]
    missing = [] if news["status"] == "NEWS_SCORED" else [f"NEWS_UNAVAILABLE: {news['note']}"]
    if not view.get("enough_data"):
        missing.insert(0, f"Market data {view.get('status')}: {view.get('bars', 0)} bars, {market_analyst.MIN_HISTORY} needed")
    missing += ["No real-time price: uses the latest stored confirmed bar.", "No AI research attached (Grok offline)."]
    p = {
        "proposal_id": uuid.uuid4().hex[:12], "run_id": run_id,
        "market_type": MARKET_CLASS[kind], "exchange_or_provider": view.get("exchange"), "symbol": sym,
        "instrument_type": {"stock": "ETF" if sym in ETFS else "STOCK", "crypto": "SPOT", "prediction": "PREDICTION_CONTRACT"}[kind],
        "signal": sig, "order_type": ORDER_TYPE if sig in ("BUY", "SELL") else None,
        "proposed_quantity": qty, "proposed_notional": qty * price if qty else None,
        "original_quantity": qty, "original_notional": qty * price if qty else None, "size_adjustment": None,
        "reference_price": price, "currency": view.get("currency"), "stop_price": stop, "take_profit_price": take,
        "analysis_time_utc": _iso(now), "data_timestamp_utc": view.get("analysis_time"), "data_mode": DATA_MODE,
        "reasoning": {
            "data_used": f"{view.get('data_source')} | {view.get('exchange')} | interval {view.get('interval')} | "
                         f"{view.get('bars', 0)} bars up to {view.get('analysis_time')} | {DATA_MODE}",
            "indicators": {k: view.get(k) for k in INDICATORS},
            "rule": RULE, "signal": sig,
            "strategy_note": s["reason"] if view.get("status") != "UNSUPPORTED" else "Unsupported market: " + view["limitations"],
            "amount": (f"target {config.MAX_POSITION_PCT:.0%} of paper equity {equity:,.2f}; stop-loss {stop:,.4f} "
                       f"(-{config.STOP_LOSS_PCT:.0%}), take-profit {take:,.4f} (+{config.TAKE_PROFIT_PCT:.0%})" if sig == "BUY"
                       else "the whole held position" if sig == "SELL" else "no order"),
            "risks": [*view.get("warnings", []), view.get("limitations")],
            "missing_or_uncertain": missing,
        },
        "market_analysis": view, "news_analysis": news,
        "supporting_research": None, "research_source": RESEARCH_SOURCE, "confidence": s.get("confidence", 0),
        "risk_decision": None, "risk_reason": "No order: HOLD / NO TRADE never reach the Risk Manager.", "risk_checks": [],
        "human_approval_status": "NOT_REQUIRED", "decided_at": None, "execution_status": "NO_ORDER",
        "created_at": _iso(now), "expires_at": None, "content_hash": None, "account_snapshot": None,
    }
    if sig in ("BUY", "SELL"):
        decision, reason, checks, allowed = _risk(conn, p, view, now, halted)
        p.update(risk_decision=decision, risk_reason=reason, risk_checks=checks)
        if decision == RISK_REJECTED:
            p.update(human_approval_status="NOT_SENT", execution_status="BLOCKED")
            _event(conn, now, "TRADE_REJECTED", sym, p["proposal_id"], signal=sig, notional=p["proposed_notional"], reason=reason)
        else:
            if allowed < qty - 1e-9:
                binding = next((c["check"][7:] for c in checks if c["detail"].endswith("BINDING")), "the limits")
                p.update(proposed_quantity=allowed, proposed_notional=allowed * price,
                         size_adjustment=f"Resized from {qty:g} ({qty * price:,.2f}) to {allowed:g} ({allowed * price:,.2f}) by the "
                                         f"Risk Manager: limited by {binding}.")
            p.update(human_approval_status=AWAITING, execution_status="WAITING_FOR_HUMAN",
                     expires_at=_iso(now + dt.timedelta(seconds=EXPIRES_SECONDS)), account_snapshot=_snapshot(conn))
            p["content_hash"] = _hash(p)
    _insert(conn, "proposals", p)
    return p


def _expire(conn, now):
    conn.execute("UPDATE proposals SET human_approval_status=?, execution_status='CANCELLED', decided_at=? "
                 "WHERE human_approval_status=? AND expires_at <= ?", (EXPIRED, _iso(now), AWAITING, _iso(now)))


def universe():
    """Supported real-data symbols, plus prediction-market symbols on the watchlist (always NO TRADE: unsupported)."""
    return [*stockdata.SYMBOLS, *(p.replace("/", "-") for p in cryptodata.PAIRS),
            *(s for s in config.WATCHLIST if market_type(s) == "prediction")]


def run(now=None, halted=False, analyze=None, symbols=None, candles=None):
    """One decision cycle: bracket exits on stored candles first, then one proposal per symbol. Never executes a BUY."""
    now = now or _now()
    check_stops(now, candles)
    views = [(analyze or market_view)(sym, now) for sym in symbols or universe()]  # read market data before taking the write lock
    scores, news = _news(views)
    run_id = uuid.uuid4().hex[:8]
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account(conn, now)
        _record_settings(conn, now)
        _expire(conn, now)
        _mark(conn, views, now)
        _daily(conn, now)  # baseline / peak / kill switch on the freshest prices, even when nothing is a BUY
        equity = _equity(conn, now)[0]
        _, plans, _ = strategy_agent.run(views, scores, _positions(conn))  # one plan per view, same order
        out = [_create(conn, run_id, s, v, news[v["symbol"]], now, halted, equity) for s, v in zip(plans, views)]
        conn.execute("COMMIT")
    return out


# ---------- Portfolio Manager ----------
def _fill(conn, p, now, approval=APPROVED, model=None):
    """Paper fill at the proposal's reference price + FEE_PCT (same math as app/broker.py), in the caller's transaction.

    Realized P&L of a SELL includes the sell fee AND the share of the entry fee, so a losing streak counts fees.
    """
    acct, held = _account(conn, now), _positions(conn).get(p["symbol"])
    qty, price = p["proposed_quantity"], p["reference_price"]
    notional = qty * price
    fee = notional * config.FEE_PCT
    before = held["qty"] if held else 0.0
    if p["signal"] == "BUY":
        cash_after, pnl, after = acct["cash"] - notional - fee, None, before + qty
        if held:
            conn.execute("UPDATE positions SET qty=?, avg_price=?, entry_fees=entry_fees+? WHERE symbol=?",
                         (after, (held["avg_price"] * before + notional) / after, fee, p["symbol"]))
        else:
            since = _ts(p["data_timestamp_utc"]) + (86400 if len(p["data_timestamp_utc"]) == 10 else 0)  # next candle / session
            _insert(conn, "positions", {
                "symbol": p["symbol"], "market_type": p["market_type"], "qty": qty, "avg_price": price, "opened_at": _iso(now),
                "mark_price": price, "mark_time": _iso(now), "mark_fresh": 1, "stop_price": p.get("stop_price"),
                "take_profit_price": p.get("take_profit_price"), "entry_fees": fee, "checked_until": since,
                "entry_proposal_id": p["proposal_id"]})
    else:
        entry_fee_share = held["entry_fees"] * qty / before
        cash_after, after = acct["cash"] + notional - fee, before - qty
        pnl = (price - held["avg_price"]) * qty - fee - entry_fee_share
        if after <= 1e-12:
            conn.execute("DELETE FROM positions WHERE symbol=?", (p["symbol"],))
        else:
            conn.execute("UPDATE positions SET qty=?, entry_fees=entry_fees-? WHERE symbol=?", (after, entry_fee_share, p["symbol"]))
    conn.execute("UPDATE account SET cash=? WHERE id=1", (cash_after,))  # CHECK (cash >= 0) aborts the whole fill if negative
    fill = {"proposal_id": p["proposal_id"], "side": p["signal"], "symbol": p["symbol"], "qty": qty, "price": price,
            "notional": notional, "fee": fee, "realized_pnl": pnl, "cash_before": acct["cash"], "cash_after": cash_after,
            "position_before": before, "position_after": max(after, 0.0), "equity_after": _equity(conn, now)[0],
            "approved_at": _iso(now), "filled_at": _iso(now), "agent": portfolio_manager.NAME,
            "fill_model": model or f"paper fill at the reference price (latest stored confirmed close) + {config.FEE_PCT:.2%} fee"}
    _insert(conn, "fills", fill)
    conn.execute("UPDATE proposals SET human_approval_status=?, execution_status='FILLED', decided_at=? WHERE proposal_id=?",
                 (approval, _iso(now), p["proposal_id"]))
    _event(conn, now, "TRADE_FILLED", p["symbol"], p["proposal_id"], side=p["signal"], qty=qty, price=price, fee=fee,
           realized_pnl=pnl, cash_after=cash_after, approval=approval)
    if pnl is not None and pnl < 0 and _streak(conn) >= config.MAX_CONSECUTIVE_LOSSES:
        until = now + dt.timedelta(hours=config.COOLDOWN_HOURS)
        reason = f"{_streak(conn)} closed losing trades in a row (limit {config.MAX_CONSECUTIVE_LOSSES})"
        conn.execute("UPDATE risk_state SET cooldown_until=?, cooldown_started_at=?, cooldown_reason=? WHERE id=1",
                     (_iso(until), _iso(now), reason))
        _event(conn, now, "COOLDOWN_STARTED", p["symbol"], p["proposal_id"], reason=reason, until=_iso(until))
    return fill


# ---------- simulated stop-loss / take-profit ----------
def candles_since(symbol, since, now):
    """(stored confirmed candles opening at/after `since` (Unix s), oldest first; data behind?; candle step or None).
    Read-only, never contacts a provider. Stocks: daily bars stamped at midnight UTC of the session date."""
    kind = market_type(symbol)
    if kind == "crypto":
        step = CRYPTO_INTERVAL * 60
        with contextlib.closing(cryptodata.connect()) as c:
            rows = c.execute("SELECT open_time, open, high, low, close, volume FROM crypto_candles WHERE exchange=? AND "
                             "market_type=? AND pair=? AND interval_min=? AND open_time>=? ORDER BY open_time LIMIT 5000",
                             (cryptodata.EXCHANGE, cryptodata.MARKET, symbol.replace("-", "/"), CRYPTO_INTERVAL, since)).fetchall()
        out = [{"open_time": r[0], "close_time": r[0] + step, "open": r[1], "high": r[2], "low": r[3], "close": r[4],
                "volume": r[5]} for r in rows]
        last_open = out[-1]["open_time"] if out else since - step
        return out, last_open < cryptodata.last_closed_open(now, CRYPTO_INTERVAL), step
    if kind == "stock":
        with contextlib.closing(stockdata.connect()) as c:
            bars = stockdata.load_bars(c, symbol)[0]
        out = [{"open_time": _ts(b["ts"]), "close_time": _ts(b["ts"]) + 86400, **{k: b[k] for k in ("open", "high", "low", "close", "volume")}}
               for b in bars if _ts(b["ts"]) >= since]
        return out, not bars or bars[-1]["ts"] < us_calendar.last_completed_session(now).isoformat(), None  # weekends are no gap
    return [], True, None


def _warn(conn, pos, now, text):
    """Log a stop-data warning once per position until the text changes (no log spam every cycle)."""
    if text == pos.get("last_warning"):
        return []
    conn.execute("UPDATE positions SET last_warning=? WHERE symbol=?", (text, pos["symbol"]))
    return [_event(conn, now, "STOP_DATA_WARNING", pos["symbol"], pos.get("entry_proposal_id"), warning=text)]


def _exit(conn, pos, price, kind, note, candle, now):
    """Bracket exit: a SELL of exactly the held quantity. Its id comes from the position, so it can't happen twice."""
    sym = pos["symbol"]
    pid = "x" + hashlib.sha256(f"{sym}|{pos['opened_at']}".encode()).hexdigest()[:11]
    p = {"proposal_id": pid, "run_id": "BRACKET", "market_type": MARKET_CLASS[market_type(sym)], "exchange_or_provider": None,
         "symbol": sym, "instrument_type": "SPOT" if market_type(sym) == "crypto" else "ETF" if sym in ETFS else "STOCK", "signal": "SELL",
         "order_type": ORDER_TYPE, "proposed_quantity": pos["qty"], "proposed_notional": pos["qty"] * price,
         "original_quantity": pos["qty"], "original_notional": pos["qty"] * price, "reference_price": price,
         "currency": ACCOUNT_CURRENCY, "analysis_time_utc": _iso(now), "data_timestamp_utc": _utc(candle["close_time"]),
         "data_mode": DATA_MODE, "research_source": RESEARCH_SOURCE, "supporting_research": None, "confidence": None,
         "reasoning": {"rule": f"{kind}: {note}", "candle": candle, "stop_price": pos["stop_price"],
                       "take_profit_price": pos["take_profit_price"], "entry_proposal_id": pos["entry_proposal_id"],
                       "strategy_note": f"{kind} exit of the bracket approved with entry {pos['entry_proposal_id']}"},
         "risk_decision": RISK_APPROVED, "risk_reason": "Risk-reducing bracket exit: sells exactly the held quantity, never more.",
         "risk_checks": [], "human_approval_status": BRACKET, "execution_status": "TRIGGERED", "created_at": _iso(now)}
    _insert(conn, "proposals", p)  # PRIMARY KEY: a second exit of the same position fails here and rolls back
    model = (f"{kind}: {note}. Fill {price:,.4f} incl. {config.SLIPPAGE_PCT:.2%} slippage" if kind.startswith("STOP")
             else f"{kind}: {note}. Limit fill {price:,.4f}") + f", + {config.FEE_PCT:.2%} fee"
    fill = _fill(conn, p, now, approval=BRACKET, model=model)
    return _event(conn, now, kind, sym, pid, note=note, candle=candle, stop_price=pos["stop_price"],
                  take_profit_price=pos["take_profit_price"], fill_price=price, qty=pos["qty"], realized_pnl=fill["realized_pnl"])


def stop_exit(c, stop, take, slippage_pct=None):
    """What one confirmed candle does to a long position with this stop / take-profit. Pure: shared with the backtest.
    Returns None (nothing hit), ("SKIP", None, why) for a candle no fill may be simulated on, or (kind, fill price, note)."""
    slip = config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    o, h, low, close, vol = (c.get(k) for k in ("open", "high", "low", "close", "volume"))
    if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (o, h, low, close, vol))
            or not 0 < low <= min(o, close) or h < max(o, close) or vol <= 0):
        return "SKIP", None, "zero/missing volume or invalid OHLC"
    hit_stop, hit_take = low <= stop, take is not None and h >= take
    if not (hit_stop or hit_take):
        return None
    if o <= stop:
        kind, base, note = "STOP_LOSS_GAP", o, f"opened at {o:,.4f}, below the stop {stop:,.4f}: filled at the open, not at the stop"
    elif take is not None and o >= take:
        kind, base, note = "TAKE_PROFIT_GAP", take, f"opened at {o:,.4f}, above the take-profit: conservatively filled at {take:,.4f}"
    elif hit_stop and hit_take:
        kind, base, note = ("STOP_LOSS_AMBIGUOUS", stop, "stop and take-profit both inside one candle; the order inside "
                            "the candle is unknown, so the worse case (stop first) is assumed")
    elif hit_stop:
        kind, base, note = "STOP_LOSS", stop, f"low {low:,.4f} touched the stop: it becomes a market order"
    else:
        kind, base, note = "TAKE_PROFIT", take, f"high {h:,.4f} touched the take-profit limit"
    return kind, (base * (1 - slip) if kind.startswith("STOP") else base), note


def _evaluate(conn, pos, bars, delayed, step, now):
    """Walk the candles after the last check. Never assumes a fill exactly at the stop on a gap, never fills on a bad
    candle, and assumes the worse case (stop first) when stop and take-profit are both inside one candle."""
    stop, take, expected, warnings = pos["stop_price"], pos["take_profit_price"], pos["checked_until"], []
    for c in bars:
        if step and c["open_time"] > expected:
            warnings.append(f"{(c['open_time'] - expected) // step} candle(s) missing before {_utc(c['open_time'])}: "
                            "a stop touched inside the gap cannot be seen")
        expected = c["close_time"]
        hit = stop_exit(c, stop, take)
        if hit and hit[0] == "SKIP":
            warnings.append(f"candle {_utc(c['open_time'])} skipped ({hit[2]}): no simulated fill on it")
            continue
        if not hit:
            continue
        kind, price, note = hit
        events = [_warn(conn, pos, now, w) for w in warnings]
        return [e for ev in events for e in ev] + [_exit(conn, pos, price, kind, note, c, now)]
    conn.execute("UPDATE positions SET checked_until=? WHERE symbol=?", (expected, pos["symbol"]))
    if delayed:
        warnings.append(f"DELAYED data: stop-loss not evaluated after {_utc(expected)} (newer closed candles are not stored)")
    return _warn(conn, pos, now, " | ".join(warnings)) if warnings else []


def check_stops(now=None, candles=None):
    """Simulated stop-loss / take-profit for every open paper position, on stored confirmed candles. Returns events."""
    now = now or _now()
    with _db() as conn:
        held = _positions(conn)
    load = candles or candles_since
    data = {s: load(s, p["checked_until"], now) for s, p in held.items() if p["stop_price"] and p["checked_until"] is not None}
    events = []
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account(conn, now)
        for sym, before in held.items():
            pos = _positions(conn).get(sym)
            if not pos or pos["opened_at"] != before["opened_at"]:
                continue  # closed meanwhile (e.g. a parallel check): never closed twice
            events += _evaluate(conn, pos, *data[sym], now) if sym in data else _warn(
                conn, pos, now, "no stop-loss price stored for this position: a stop can't be simulated")
        if events:
            _daily(conn, now)  # a losing exit can push the drawdown over the kill-switch limit
        conn.execute("COMMIT")
    return events


# ---------- human approval ----------
def approve(pid, content_hash, now=None, halted=False, analyze=None):
    """Human approval. Re-checks expiry, content, cash/positions, fresh market data and every risk rule, then fills once.

    Returns (ok, fill or message). A double click: the second call waits for the first one's lock, then sees the
    proposal is no longer AWAITING. UNIQUE(fills.proposal_id) is a second guard.
    """
    now = now or _now()
    with _db() as conn:
        p = _proposal(conn, pid)
    if not p:
        return False, "Unknown proposal."
    view = (analyze or market_view)(p["symbol"], now) if p["human_approval_status"] == AWAITING else None  # fresh data, before the lock
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire(conn, now)
        p = _proposal(conn, pid)
        status = p["human_approval_status"]
        if status != AWAITING:
            conn.execute("COMMIT")
            why = {EXPIRED: "it expired: create a new proposal", "NOT_SENT": "the Risk Manager rejected it; it was never sent for approval",
                   "NOT_REQUIRED": "HOLD / NO TRADE never creates an order"}.get(status, "already handled")
            return False, f"Not executed: proposal is {status} / {p['execution_status']} ({why})."
        problems = []
        if content_hash != p["content_hash"] or _hash(p) != p["content_hash"]:
            problems.append("proposal content changed since it was shown (content hash mismatch)")
        if p["account_snapshot"] and _snapshot(conn) != p["account_snapshot"]:
            problems.append("paper cash or positions changed since this proposal was made: run a new decision cycle")
        if view.get("analysis_time") != p["data_timestamp_utc"] or view.get("price") != p["reference_price"]:
            problems.append(f"market data changed since the proposal ({p['data_timestamp_utc']} -> {view.get('analysis_time')}): "
                            "create a new proposal")
        _mark(conn, [view], now)
        decision, reason, checks, _ = _risk(conn, p, view, now, halted, at_approval=True)
        if decision == RISK_REJECTED:
            problems.append(reason)
        if problems:
            conn.execute("UPDATE proposals SET human_approval_status=?, risk_decision=?, risk_reason=?, risk_checks=?, "
                         "execution_status='BLOCKED', decided_at=? WHERE proposal_id=?",
                         (APPROVED, RISK_REJECTED, "At approval: " + "; ".join(problems), json.dumps(checks), _iso(now), pid))
            _event(conn, now, "TRADE_BLOCKED_AT_APPROVAL", p["symbol"], pid, problems=problems)
            conn.execute("COMMIT")
            return False, "Approval received but NOT executed. Re-check failed: " + "; ".join(problems)
        fill = _fill(conn, p, now)
        conn.execute("COMMIT")
    return True, fill


def reject(pid, now=None):
    now = now or _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire(conn, now)
        p = _proposal(conn, pid)
        if not p or p["human_approval_status"] != AWAITING:
            conn.execute("COMMIT")
            return False, f"Not changed: proposal is {p['human_approval_status'] if p else 'unknown'}."
        conn.execute("UPDATE proposals SET human_approval_status=?, execution_status='CANCELLED', decided_at=? WHERE proposal_id=?",
                     (REJECTED, _iso(now), pid))
        _event(conn, now, "TRADE_REJECTED_BY_HUMAN", p["symbol"], pid, signal=p["signal"], notional=p["proposed_notional"])
        conn.execute("COMMIT")
    return True, "Rejected. No order."


def reset_kill_switch(reviewer, note, confirm, triggered_at, now=None):
    """Clear a latched kill switch after an explicit human review. Server-side checks; never automatic.

    Requires: the switch is active, the reviewer typed RESET_PHRASE, a name and a review note, the trigger time they
    reviewed is the current one, and equity can be valued on fresh prices. The peak is re-based to that equity (the
    reviewer accepts the loss as the new starting point); otherwise the switch would fire again at once.
    """
    now = now or _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        acct, st = _account(conn, now), _risk_state(conn)
        equity, stale = _equity(conn, now)
        problems = [msg for bad, msg in (
            (not st["kill_switch_active"], "the kill switch is not active"),
            (confirm != RESET_PHRASE, f'type the confirmation phrase exactly: "{RESET_PHRASE}"'),
            (not str(reviewer or "").strip() or len(str(note or "").strip()) < 10, "a reviewer name and a review note (10+ characters) are required"),
            (st["kill_switch_active"] and triggered_at != st["kill_triggered_at"], "the trigger you reviewed is not the current one: reload and review again"),
            (bool(stale), f"no fresh price for {', '.join(stale)}: run a decision cycle first so the review uses current equity"),
        ) if bad]
        if problems:
            _event(conn, now, "KILL_SWITCH_RESET_REFUSED", reviewer=reviewer, problems=problems)
            conn.execute("COMMIT")
            return False, "Kill switch NOT reset: " + "; ".join(problems)
        conn.execute("UPDATE risk_state SET kill_switch_active=0, kill_triggered_at=NULL, kill_reason=NULL, kill_equity=NULL, "
                     "kill_peak=NULL WHERE id=1")
        conn.execute("UPDATE account SET peak_equity=? WHERE id=1", (equity,))
        _event(conn, now, "KILL_SWITCH_RESET", reviewer=str(reviewer).strip(), note=str(note).strip(), triggered_at=st["kill_triggered_at"],
               trigger_reason=st["kill_reason"], equity=equity, old_peak=acct["peak_equity"], new_peak=equity)
        conn.execute("COMMIT")
    return True, f"Kill switch reset by {str(reviewer).strip()}. Peak re-based to {equity:,.2f}."


# ---------- Performance Analyst + dashboard payload ----------
def _performance(conn, now, acct):
    fills = [dict(r) for r in conn.execute("SELECT * FROM fills ORDER BY fill_id")]
    positions = _positions(conn)
    equity, stale = _equity(conn, now)
    trades = [{"side": f["side"], "pnl": f["realized_pnl"], "fee": f["fee"]} for f in fills]
    curve = [{"equity": acct["starting_cash"]}] + [{"equity": f["equity_after"]} for f in fills] + [{"equity": equity}]
    stats, note = performance_analyst.run(trades, curve, acct["starting_cash"])
    counts = dict(conn.execute("SELECT human_approval_status || '|' || execution_status || '|' || signal, COUNT(*) "
                               "FROM proposals GROUP BY 1").fetchall())

    def n(approval=None, execution=None, signal=None):
        return sum(c for k, c in counts.items() if all(want is None or got == want for got, want in
                                                      zip(k.split("|"), (approval, execution, signal))))

    out = {
        "equity": round(equity, 2), "cash": round(acct["cash"], 2), "starting_cash": acct["starting_cash"],
        "prices_fresh": not stale, "stale_prices": stale,
        "realized_pnl": stats["realized_pnl"],
        "unrealized_pnl": round(sum((p["mark_price"] - p["avg_price"]) * p["qty"] for p in positions.values()), 2),
        "total_return_pct": stats["total_return_pct"], "trade_count": len(fills), "closed_trades": stats["closed_trades"],
        "win_rate_pct": stats["win_rate_pct"], "max_drawdown_pct": stats["max_drawdown_pct"] if fills else None,
        "fees_paid": stats["fees_paid"],
        "approved_orders": n(APPROVED, "FILLED"), "risk_rejected": n("NOT_SENT"), "human_rejected": n(REJECTED),
        "blocked_at_approval": n(APPROVED, "BLOCKED"), "expired": n(EXPIRED), "waiting": n(AWAITING),
        "bracket_exits": n(BRACKET, "FILLED"), "no_trade": n(signal="NO TRADE"), "hold": n(signal="HOLD"), "note": note,
    }
    return {k: "N/A" if v is None else v for k, v in out.items()}


def _decode(row):
    return {k: json.loads(v) if k in JSON_FIELDS and v else v for k, v in dict(row).items()}


def _risk_summary(conn, now, acct):
    st = _risk_state(conn)
    equity, stale = _equity(conn, now)
    value = {s: r["qty"] * (r["mark_price"] or r["avg_price"]) for s, r in _positions(conn).items()}
    by = {cls: sum(v for s, v in value.items() if MARKET_CLASS[market_type(s)] == cls) for cls in ("STOCK", "CRYPTO_SPOT")}
    pct = (lambda x: round(x / equity * 100, 2)) if equity > 0 else (lambda x: None)
    today = acct["day_date"] == now.date().isoformat()
    peak = acct["peak_equity"]
    events = [{**dict(r), "detail": json.loads(r["detail"])} for r in conn.execute("SELECT * FROM risk_events ORDER BY event_id DESC LIMIT 300")]
    exits = ("STOP_LOSS", "STOP_LOSS_GAP", "STOP_LOSS_AMBIGUOUS", "TAKE_PROFIT", "TAKE_PROFIT_GAP", "STOP_DATA_WARNING")
    return {
        "equity": round(equity, 2), "prices_fresh": not stale, "stale_prices": stale,
        "exposure": {"total_pct": pct(sum(value.values())), "STOCK_pct": pct(by["STOCK"]), "CRYPTO_SPOT_pct": pct(by["CRYPTO_SPOT"]),
                     "PREDICTION": "UNSUPPORTED", "limits_pct": {k: v * 100 for k, v in _market_limits().items()},
                     "total_limit_pct": config.MAX_TOTAL_EXPOSURE_PCT * 100, "basis": "each position once, at its latest analyzed price"},
        "daily": {"timezone": DAY_TIMEZONE, "day_starts": "00:00 UTC", "date": acct["day_date"], "baseline": acct["day_start_equity"],
                  "pnl": round(equity - acct["day_start_equity"], 2) if today and not stale else None,
                  "pnl_pct": round((equity / acct["day_start_equity"] - 1) * 100, 2) if today and not stale and acct["day_start_equity"] else None,
                  "limit_pct": config.MAX_DAILY_LOSS_PCT * 100,
                  "basis": "equity = cash after all fees + open positions at the latest analyzed price (realized + unrealized P&L)"},
        "drawdown": {"peak": round(peak, 2), "pct": round((peak - equity) / peak * 100, 2) if peak else None,
                     "limit_pct": config.MAX_DRAWDOWN_PCT * 100},
        "per_trade_risk_pct": config.RISK_PER_TRADE_PCT * 100, "stop_loss_pct": config.STOP_LOSS_PCT * 100,
        "take_profit_pct": config.TAKE_PROFIT_PCT * 100, "slippage_pct": config.SLIPPAGE_PCT * 100,
        "losing_streak": _streak(conn), "max_consecutive_losses": config.MAX_CONSECUTIVE_LOSSES,
        "cooldown": {"active": bool(st["cooldown_until"]), "until": st["cooldown_until"], "started_at": st["cooldown_started_at"],
                     "reason": st["cooldown_reason"], "hours": config.COOLDOWN_HOURS},
        "kill_switch": {"active": bool(st["kill_switch_active"]), "triggered_at": st["kill_triggered_at"], "reason": st["kill_reason"],
                        "equity_at_trigger": st["kill_equity"], "peak_at_trigger": st["kill_peak"], "reset_phrase": RESET_PHRASE},
        "events": events[:80],
        "stop_events": [e for e in events if e["kind"] in exits][:40],
        "kill_switch_history": [e for e in events if e["kind"].startswith("KILL_SWITCH")][:20],
        "settings_history": [{"recorded_at": r[0], "settings": json.loads(r[1])} for r in
                             conn.execute("SELECT recorded_at, settings FROM risk_settings ORDER BY id DESC LIMIT 10")],
        "rejected": [_decode(r) for r in conn.execute(
            "SELECT * FROM proposals WHERE risk_decision='RISK_REJECTED' OR human_approval_status=? "
            "ORDER BY COALESCE(decided_at, created_at) DESC LIMIT 30", (REJECTED,))],
    }


def state(now=None, limit=60):
    now = now or _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        acct = _account(conn, now)
        _record_settings(conn, now)
        _expire(conn, now)
        _daily(conn, now)
        acct = _account(conn, now)
        conn.execute("COMMIT")
        last_run = conn.execute("SELECT run_id FROM proposals WHERE run_id<>'BRACKET' ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return {
            "mode": "SIMULATION ONLY",
            "notice": "Paper decisions on stored historical data. Approving writes a simulated fill to data/decisions.db. "
                      "No order is ever sent to an exchange, broker or wallet.",
            "account": acct, "positions": list(_positions(conn).values()),
            "latest_run": [_decode(r) for r in conn.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY rowid",
                                                            (last_run[0] if last_run else "",))],
            "history": [_decode(r) for r in conn.execute("SELECT * FROM proposals ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))],
            "fills": [dict(r) for r in conn.execute("SELECT * FROM fills ORDER BY fill_id DESC LIMIT 50")],
            "performance": _performance(conn, now, acct),
            "risk": _risk_summary(conn, now, acct),
            "server_time_utc": _iso(now),
            "settings": {"max_position_pct": config.MAX_POSITION_PCT, "max_open_positions": config.MAX_OPEN_POSITIONS,
                         "max_total_exposure_pct": config.MAX_TOTAL_EXPOSURE_PCT,
                         "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT, "max_drawdown_pct": config.MAX_DRAWDOWN_PCT,
                         "max_crypto_exposure_pct": config.MAX_CRYPTO_EXPOSURE_PCT, "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
                         "stop_loss_pct": config.STOP_LOSS_PCT, "take_profit_pct": config.TAKE_PROFIT_PCT,
                         "slippage_pct": config.SLIPPAGE_PCT, "max_consecutive_losses": config.MAX_CONSECUTIVE_LOSSES,
                         "cooldown_hours": config.COOLDOWN_HOURS,
                         "review_below_confidence": config.REVIEW_BELOW_CONFIDENCE, "fee_pct": config.FEE_PCT,
                         "expires_seconds": EXPIRES_SECONDS, "abnormal_jump_pct": stockdata.SPLIT_GAP,
                         "crypto_interval_min": CRYPTO_INTERVAL, "order_type": ORDER_TYPE},
        }
