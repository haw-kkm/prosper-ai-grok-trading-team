"""Lesson 11: Backtesting Lab. SIMULATION / HISTORICAL BACKTEST ONLY: nothing here can trade.

Replays stored candles through the existing agents, one candle at a time:
    Market Analyst (market_analyst.run on CLOSED candles only) + News Analyst (no point-in-time news -> NEWS_UNAVAILABLE)
    -> Strategy Agent (strategy_agent.run, unchanged rules) -> Risk Manager (risk_manager.review + the Lesson 10 limits and
    stop rule, shared via decisions.buy_limits / decisions.stop_exit) -> backtest approval policy -> simulated fill
    -> Performance Analyst (performance_analyst.run + drawdown / Sharpe from the same equity curve).

- Every train / test segment has its own in-memory account and risk state (daily loss, cooldown, kill switch). Nothing reads
  or writes data/decisions.db, and the backtest approval policy exists only in this file: it can't reach the live approve() path.
- Market data is opened READ-ONLY and streamed in chunks over the indexed primary key (1-minute history never sits in memory).
- No look-ahead: a decision at a candle's close sees only candles closed by then; its order fills at the NEXT candle's open
  (+ slippage); higher-timeframe context uses only higher candles that had closed by then; candles that close after "now"
  are never used; nothing is normalized with whole-period statistics.
- DEMO synthetic data exists only when source="DEMO" is chosen; every DEMO result is labeled DEMO.

    python -m app.backtest run --symbol BTC-USD --interval 60 --policy SIMULATED_APPROVAL
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import random
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque

from . import config, cryptodata, decisions, stockdata, us_calendar
from .agents import market_analyst, news_analyst, performance_analyst, risk_manager, strategy_agent
from .market import market_type

RUNS_DIR = config.DATA_DIR / "backtests"
POLICIES = ("PROPOSAL_ONLY", "SIMULATED_APPROVAL")
MARKETS = {"CRYPTO_SPOT": {"symbols": [p.replace("/", "-") for p in cryptodata.PAIRS], "intervals": [1, 5, 15, 60, 240, 1440]},
           "STOCK": {"symbols": list(stockdata.SYMBOLS), "intervals": [1440]},
           "PREDICTION": {"symbols": [], "intervals": []}}
MIN_SEGMENT = 50       # candles a train or test segment needs to be evaluated at all
CHART_POINTS = 800     # stored chart points per segment (metrics always use every candle)
MAX_LISTED = 2000      # stored proposals / events per segment (counts always cover everything)
CHUNK = 20_000         # candles per database fetch
WINDOW = market_analyst.MIN_HISTORY
CODE_FILES = ("backtest.py", "decisions.py", "agents/strategy_agent.py", "agents/risk_manager.py",
              "agents/market_analyst.py", "agents/performance_analyst.py")
NOTES = [
    "Strategy = the existing Strategy Agent rules (BUY if trend score >= 0.45 and flat, SELL if held and <= -0.45). Nothing was "
    "fitted on Train or Test. If the strategy is changed after looking at Test, Test is no longer unseen: a new untouched period is needed.",
    "News: NEWS_UNAVAILABLE for every candle. There is no point-in-time news archive; today's data/news.json and Grok are never "
    "used for past dates.",
    "Orders fill at the NEXT candle's open + slippage, never at the signal candle's close. Ending equity marks an open position at "
    "the last close without exit costs (the same for Buy & Hold).",
    "Buy & Hold invests 100% of equity while the strategy is capped at 15% per position: compare drawdown and exposure, not only return.",
]
_lock = threading.Lock()
_progress = {}


def _day(unix):
    return dt.datetime.fromtimestamp(unix, dt.timezone.utc).date()


def _iso(unix):
    return dt.datetime.fromtimestamp(unix, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _candle(open_time, step, o, h, low, c, v, origin=None):
    return {"open_time": open_time, "close_time": open_time + step, "open": o, "high": h, "low": low, "close": c, "volume": v, "origin": origin}


def _tradable(c):
    vals = (c["open"], c["high"], c["low"], c["close"], c["volume"])
    return (all(isinstance(x, (int, float)) and math.isfinite(x) for x in vals) and 0 < c["low"] <= min(c["open"], c["close"])
            and c["high"] >= max(c["open"], c["close"]) and c["volume"] > 0)


# ---------- data sources ----------
class ListSource:
    """Candles held in memory: stocks (a few thousand daily bars), DEMO data and tests."""

    def __init__(self, candles, step, meta):
        self.candles, self.step, self.meta = sorted(candles, key=lambda c: c["open_time"]), step, meta

    def _range(self, start, end):
        return [c for c in self.candles if (start is None or c["open_time"] >= start) and (end is None or c["open_time"] <= end)]

    def count(self, start, end):
        r = self._range(start, end)
        return len(r), r[0]["open_time"] if r else None, r[-1]["open_time"] if r else None

    def kth(self, start, end, k):
        return self._range(start, end)[k]["open_time"]

    def before(self, t, n):
        return [c for c in self.candles if c["open_time"] < t][-n:] if n else []

    def iter(self, start, end):
        yield from self._range(start, end)


class SqlSource:
    """Kraken spot candles from data/market_data.db, opened READ-ONLY, streamed in chunks over the primary-key index."""
    COLS = "open_time, open, high, low, close, volume, origin"

    def __init__(self, pair, interval):
        self.step, self.args = interval * 60, (cryptodata.EXCHANGE, cryptodata.MARKET, pair, interval)
        self.where = "exchange=? AND market_type=? AND pair=? AND interval_min=?"
        self.meta = {"market": "CRYPTO_SPOT", "provider": "Kraken spot (exchange KRAKEN)", "pair": pair, "interval_min": interval,
                     "price_type": "RAW exchange prices (no split/dividend adjustment applies to spot crypto)",
                     "source": "data/market_data.db table crypto_candles: Kraken official OHLCVT archive (origin CSV, imported, not "
                               "re-verified here) + Kraken public REST OHLC (origin API)"}

    def _conn(self):
        return contextlib.closing(sqlite3.connect(f"file:{stockdata.DB_PATH}?mode=ro", uri=True))  # read-only: never changes data

    def _between(self, start, end):
        q, a = self.where, list(self.args)
        if start is not None:
            q, a = q + " AND open_time >= ?", a + [start]
        if end is not None:
            q, a = q + " AND open_time <= ?", a + [end]
        return q, a

    def _row(self, r):
        return _candle(r[0], self.step, *r[1:6], r[6])

    def count(self, start, end):
        q, a = self._between(start, end)
        try:
            with self._conn() as c:
                return tuple(c.execute(f"SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM crypto_candles WHERE {q}", a).fetchone())
        except sqlite3.OperationalError:  # no market database yet
            return 0, None, None

    def kth(self, start, end, k):
        q, a = self._between(start, end)
        with self._conn() as c:
            return c.execute(f"SELECT open_time FROM crypto_candles WHERE {q} ORDER BY open_time LIMIT 1 OFFSET ?", a + [k]).fetchone()[0]

    def before(self, t, n):
        with self._conn() as c:
            rows = c.execute(f"SELECT {self.COLS} FROM crypto_candles WHERE {self.where} AND open_time < ? ORDER BY open_time DESC "
                             "LIMIT ?", (*self.args, t, n)).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def iter(self, start, end):
        q, a = self._between(start, end)
        with self._conn() as c:
            cur = c.execute(f"SELECT {self.COLS} FROM crypto_candles WHERE {q} ORDER BY open_time", a)
            while rows := cur.fetchmany(CHUNK):
                yield from (self._row(r) for r in rows)


def stock_source(symbol):
    """Stored RAW daily bars (Lesson 6). A bar is stamped at 00:00 UTC of its session date; it is final after the close."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{stockdata.DB_PATH}?mode=ro", uri=True)) as c:
            rows = c.execute("SELECT ts, open, high, low, close, volume, provider, origin FROM bars WHERE asset_class='stock' AND "
                             "symbol=? AND interval='daily' AND data_type='RAW' ORDER BY ts", (symbol,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    candles = [_candle(decisions._ts(r[0]), 86400, *r[1:6], f"{r[7]}:{r[6]}") for r in rows]
    return ListSource(candles, 86400, {"market": "STOCK", "provider": ", ".join(sorted({r[6] for r in rows})) or None, "pair": symbol,
                                       "interval_min": 1440, "price_type": "RAW daily bars (NOT split-adjusted)",
                                       "source": "data/market_data.db table bars (Alpha Vantage API or your CSV)"})


def demo_source(n=3000, step=3600, seed=11):
    """DEMO ONLY: a seeded synthetic random walk. Never market data; every result made from it is labeled DEMO."""
    rng, price, drift, start, out = random.Random(seed), 100.0, 0.0, 1_700_000_000 // step * step, []
    for i in range(n):
        drift = 0.98 * drift + rng.gauss(0, 0.0004)
        o, c = price, max(0.01, price * math.exp(drift + rng.gauss(0, 0.006)))
        out.append(_candle(start + i * step, step, o, max(o, c) * (1 + abs(rng.gauss(0, 0.002))),
                           min(o, c) * (1 - abs(rng.gauss(0, 0.002))), c, 10 + rng.random() * 5, "DEMO"))
        price = c
    return ListSource(out, step, {"market": "CRYPTO_SPOT", "provider": "DEMO", "pair": "DEMO-USD", "interval_min": step // 60,
                                  "price_type": "SYNTHETIC", "source": "DEMO synthetic random walk (seeded). NOT market data."})


class _Higher:
    """Context from a higher timeframe: only candles that had CLOSED at the moment asked. An open candle is never used."""

    def __init__(self, src, start, end, interval):
        self.interval, self.it = interval, iter(src.iter(start, end))
        self.nxt, self.closes, self.as_of, self.cache = next(self.it, None), deque(maxlen=WINDOW), None, None

    def at(self, t):
        moved = False
        while self.nxt and self.nxt["close_time"] <= t:
            self.closes.append(self.nxt["close"])
            self.as_of, self.nxt, moved = self.nxt["close_time"], next(self.it, None), True
        if moved:
            if len(self.closes) >= WINDOW:
                ind = market_analyst._indicators(list(self.closes))
                self.cache = {"trend": ind["trend"], "trend_score": ind["trend_score"], "as_of": _iso(self.as_of)}
            else:
                self.cache = {"status": "INSUFFICIENT DATA", "as_of": _iso(self.as_of)}
        return self.cache


# ---------- settings ----------
def normalize(raw):
    """Validated run settings (unknown keys ignored). Raises ValueError with a plain message."""
    c = {"market": "CRYPTO_SPOT", "symbol": "BTC-USD", "interval": 60, "start": None, "end": None, "train_ratio": 0.7,
         "starting_equity": config.STARTING_CASH, "fee_pct": None, "slippage_pct": None, "approval_policy": "PROPOSAL_ONLY",
         "higher": [240, 1440], "source": "REAL"}
    c.update({k: v for k, v in (raw or {}).items() if k in c and v not in (None, "")})
    if c["market"] not in MARKETS:
        raise ValueError(f"market must be one of {list(MARKETS)}")
    if c["source"] not in ("REAL", "DEMO"):
        raise ValueError("source must be REAL or DEMO")
    if c["approval_policy"] not in POLICIES:
        raise ValueError(f"approval_policy must be one of {list(POLICIES)} (SIMULATED_APPROVAL only when chosen explicitly)")
    c["symbol"] = "DEMO-USD" if c["source"] == "DEMO" else str(c["symbol"]).upper().replace("/", "-")
    try:
        c["interval"], c["train_ratio"], c["starting_equity"] = int(c["interval"]), float(c["train_ratio"]), float(c["starting_equity"])
        c["higher"] = sorted({int(h) for h in c["higher"]})
    except (TypeError, ValueError):
        raise ValueError("interval, higher, train_ratio and starting_equity must be numbers") from None
    if c["market"] != "PREDICTION" and c["source"] == "REAL":
        if c["symbol"] not in MARKETS[c["market"]]["symbols"]:
            raise ValueError(f"symbol must be one of {MARKETS[c['market']]['symbols']}")
        if c["interval"] not in MARKETS[c["market"]]["intervals"]:
            raise ValueError(f"interval (minutes) must be one of {MARKETS[c['market']]['intervals']}")
    if c["interval"] <= 0:
        raise ValueError("interval must be a positive number of minutes")
    c["higher"] = [h for h in c["higher"] if h in MARKETS["CRYPTO_SPOT"]["intervals"] and h > c["interval"]
                   and c["market"] == "CRYPTO_SPOT" and c["source"] == "REAL"]
    if not 0.1 <= c["train_ratio"] <= 0.9:
        raise ValueError("train_ratio must be between 0.1 and 0.9")
    if not 0 < c["starting_equity"] <= 1e9:
        raise ValueError("starting_equity must be above 0")
    costs = config.BACKTEST_COSTS.get("STOCK" if c["market"] == "STOCK" else "CRYPTO_SPOT")
    for key in ("fee_pct", "slippage_pct"):
        c[key] = costs[key] if c[key] is None else float(c[key])
        if not 0 <= c[key] <= 0.05:
            raise ValueError(f"{key} must be between 0 and 0.05 (0-5%)")
    c["cost_source"] = costs["source"] if (c["fee_pct"], c["slippage_pct"]) == (costs["fee_pct"], costs["slippage_pct"]) \
        else "USER OVERRIDE for this run: " + costs["source"]
    for key, end in (("start", False), ("end", True)):
        if c[key] is not None:
            try:
                day = dt.date.fromisoformat(str(c[key])[:10])
            except ValueError:
                raise ValueError(f"{key} must be a date like 2024-01-31") from None
            c[key] = str(day)
    if c["start"] and c["end"] and c["start"] > c["end"]:
        raise ValueError("start must be before end")
    return c


def _bounds(c):
    to_unix = lambda d, extra=0: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp()) + extra  # noqa: E731
    return (to_unix(c["start"]) if c["start"] else None), (to_unix(c["end"], 86399) if c["end"] else None)


def code_version():
    h = hashlib.sha256()
    for name in CODE_FILES:
        h.update((config.ROOT / "app" / name).read_bytes())
    return h.hexdigest()[:12]


# ---------- one isolated segment ----------
class _Book:
    """Paper account + Lesson 10 risk state for ONE backtest segment. In memory only; never shared, never stored live."""

    def __init__(self, equity, fee, slip):
        self.cash, self.fee, self.slip, self.pos = equity, fee, slip, None
        self.peak, self.kill, self.cooldown_until, self.streak = equity, None, None, 0
        self.trades, self.events, self.event_counts = [], [], Counter()

    def event(self, t, kind, **detail):
        self.event_counts[kind] += 1
        if len(self.events) < MAX_LISTED:
            self.events.append({"time": _iso(t), "kind": kind, **detail})

    def equity(self, price):
        return self.cash + (self.pos["qty"] * price if self.pos else 0.0)

    def buy(self, t, qty, open_price, order):
        price = open_price * (1 + self.slip)
        fee = qty * price * self.fee
        self.cash -= qty * price + fee
        self.pos = {"entry_time": t, "qty": qty, "price": price, "fee": fee, "slippage": qty * open_price * self.slip,
                    "stop": order["stop"], "take": order["take"], "signal_time": order["time"], "signal_price": order["price"],
                    "risk_decision": order["risk_decision"]}

    def sell(self, t, price, slippage, kind, note):
        p = self.pos
        fee = p["qty"] * price * self.fee
        self.cash += p["qty"] * price - fee
        pnl = (price - p["price"]) * p["qty"] - fee - p["fee"]
        self.trades.append({"signal_time": _iso(p["signal_time"]), "entry_time": _iso(p["entry_time"]), "entry_price": p["price"],
                            "qty": p["qty"], "stop": p["stop"], "take": p["take"], "exit_time": _iso(t), "exit_price": price,
                            "exit_kind": kind, "exit_note": note, "fees": p["fee"] + fee, "slippage_cost": p["slippage"] + slippage,
                            "pnl": pnl, "return_pct": pnl / (p["qty"] * p["price"]) * 100,
                            "holding_hours": (t - p["entry_time"]) / 3600, "risk_decision": p["risk_decision"]})
        self.pos = None
        self.streak = self.streak + 1 if pnl < 0 else 0
        if pnl < 0 and self.streak >= config.MAX_CONSECUTIVE_LOSSES:
            self.cooldown_until = t + config.COOLDOWN_HOURS * 3600
            self.event(t, "COOLDOWN_STARTED", losses_in_a_row=self.streak, until=_iso(self.cooldown_until))


def _segment(name, src, higher, start, end, n, c, now, run_id=None):
    sym, kind = c["symbol"], market_type(c["symbol"])
    fee, slip, e0 = c["fee_pct"], c["slippage_pct"], c["starting_equity"]
    book = _Book(e0, fee, slip)
    warm = src.before(start, WINDOW)
    closes, prev = deque((w["close"] for w in warm), maxlen=WINDOW), (warm[-1] if warm else None)
    ctx = {h: _Higher(s, start - (WINDOW + 5) * h * 60, end, h) for h, s in higher.items()}
    pending, day, day_start, last_eq = None, None, e0, e0
    signals, risk_counts, reasons, origins, proposals, chart = Counter(), Counter(), Counter(), Counter(), [], []
    gaps, missing, bad, in_market, max_dd = [], 0, 0, 0, 0.0
    bh_qty, bh_cost, bh_peak, bh_max_dd, bh_eq = None, 0.0, e0, 0.0, e0
    nr, mean, m2, down2 = 0, 0.0, 0.0, 0.0
    stride, i, first, last = max(1, n // CHART_POINTS), -1, None, None
    bpy = 252 if kind == "stock" else 365 * 86400 / src.step

    for cd in src.iter(start, end):
        if cd["close_time"] > now:
            break  # an unconfirmed candle is never used
        i, t0, t1 = i + 1, cd["open_time"], cd["close_time"]
        first, last = first or cd, cd
        gap = bool(prev and t0 > prev["close_time"])
        if gap:
            lost = (t0 - prev["close_time"]) // src.step if kind != "stock" else len(
                us_calendar.trading_days(_day(prev["close_time"]), _day(t0))) - 1
            gap = lost > 0
            if gap:
                missing += lost
                if len(gaps) < 50:
                    gaps.append({"after": _iso(prev["close_time"]), "before": _iso(t0), "missing_candles": lost})
        origins[cd.get("origin")] += 1
        good = _tradable(cd) and (kind != "stock" or us_calendar.is_trading_day(_day(t0)))
        bad += not good
        today = _day(t0)
        if today != day:  # daily-loss baseline = equity at 00:00 UTC (the previous close)
            day, day_start = today, last_eq
        if bh_qty is None and good:  # Buy & Hold: same equity, same costs, from the first tradable open
            bh_price = cd["open"] * (1 + slip)
            bh_qty = e0 / (bh_price * (1 + fee))
            bh_cost = bh_qty * bh_price * fee + bh_qty * cd["open"] * slip

        # 1. the order from the previous close fills at this open (the next tradable moment), or not at all
        if pending:
            order, pending = pending, None
            if not good:
                book.event(t0, "NO_FILL", side=order["side"], why="next candle has no trades or invalid prices")
            elif order["side"] == "SELL" and book.pos:
                book.sell(t0, cd["open"] * (1 - slip), book.pos["qty"] * cd["open"] * slip, "SIGNAL_SELL", "Strategy SELL, next open")
            elif order["side"] == "BUY" and not book.pos:
                why = None  # approval-time re-check: risk state now, size clipped at the fill price (never increased)
                eq, price = book.equity(cd["open"]), cd["open"] * (1 + slip)
                if book.kill or (book.cooldown_until and t0 < book.cooldown_until):
                    why = "kill switch" if book.kill else "cooldown"
                else:
                    [v], _ = risk_manager.review([{"symbol": sym, "action": "BUY", "price": price, "confidence": order["confidence"]}],
                                                 {}, eq, book.cash, day_start, book.peak)
                    if v["verdict"] == "REJECTED":
                        why = v["risk_note"]
                    else:
                        caps = decisions.buy_limits(sym, price, order["stop"], eq, book.cash, {}, v["qty"], fee, slip)
                        qty = decisions._unit(kind, min(order["qty"], *(x[0] for x in caps.values())))
                        if qty < (1 if kind == "stock" else 1e-8) or qty * price < decisions.MIN_ORDER_USD:
                            why = "below the minimum order at the fill price"
                if why:
                    book.event(t0, "NO_FILL", side="BUY", why=f"re-check at fill time failed: {why}")
                else:
                    book.buy(t0, qty, cd["open"], order)

        # 2. stop-loss / take-profit inside this candle (shared Lesson 10 rule: gaps fill at the open, ties = stop first)
        if book.pos:
            hit = decisions.stop_exit(cd, book.pos["stop"], book.pos["take"], slip)
            if hit and hit[0] == "SKIP":
                book.event(t1, "STOP_NOT_EVALUATED", why=hit[2])
            elif hit:
                kind_hit, price, note = hit
                base = price / (1 - slip) if kind_hit.startswith("STOP") else price
                book.sell(t0, price, book.pos["qty"] * (base - price), kind_hit, note)
                book.event(t0, kind_hit, price=price, note=note)

        # 3. mark at the close: equity curve, peak, drawdown kill switch, Buy & Hold
        eq = book.equity(cd["close"])
        book.peak = max(book.peak, eq)
        dd = (book.peak - eq) / book.peak
        max_dd = max(max_dd, dd)
        if dd >= config.MAX_DRAWDOWN_PCT and not book.kill:
            book.kill = t1
            book.event(t1, "KILL_SWITCH_TRIGGERED", drawdown_pct=dd * 100, equity=eq, peak=book.peak)
        r = eq / last_eq - 1
        nr += 1
        delta = r - mean
        mean += delta / nr
        m2 += delta * (r - mean)
        down2 += min(r, 0.0) ** 2
        last_eq = eq
        in_market += book.pos is not None
        if bh_qty is not None:
            bh_eq = bh_qty * cd["close"]
            bh_peak = max(bh_peak, bh_eq)
            bh_max_dd = max(bh_max_dd, (bh_peak - bh_eq) / bh_peak)
        if i % stride == 0:
            chart.append({"t": _iso(t1), "eq": round(eq, 2), "bh": round(bh_eq, 2), "close": cd["close"], "dd": round(dd * 100, 3)})

        # 4. decision at the close: only what was known at t1
        closes.append(cd["close"])
        if book.cooldown_until and t1 >= book.cooldown_until:
            book.event(t1, "COOLDOWN_ENDED")
            book.cooldown_until = None
        reports, _ = market_analyst.run({sym: {"type": kind, "history": list(closes), "price": cd["close"]}})
        _, plans, _ = strategy_agent.run(reports, {sym: 0.0}, {sym: book.pos} if book.pos else {})
        sig = plans[0]["action"]
        signals[sig] += 1
        if sig in ("BUY", "SELL"):
            order = {"side": sig, "time": t1, "price": cd["close"], "confidence": plans[0]["confidence"], "stop": None, "take": None}
            why, decision = None, "RISK_APPROVED"
            if sig == "BUY":
                order["stop"], order["take"] = cd["close"] * (1 - config.STOP_LOSS_PCT), cd["close"] * (1 + config.TAKE_PROFIT_PCT)
                jump = abs(cd["close"] / prev["close"] - 1) if prev else 0
                if gap:
                    why = "data gap right before this candle"
                elif not good:
                    why = "signal candle has no trades or invalid prices"
                elif jump > stockdata.SPLIT_GAP:
                    why = f"abnormal price jump {jump:.0%}"
                elif book.kill:
                    why = "drawdown kill switch active"
                elif book.cooldown_until:
                    why = "losing-streak cooldown active"
                else:
                    [v], _ = risk_manager.review([{"symbol": sym, "action": "BUY", "price": cd["close"], "confidence": order["confidence"]}],
                                                 {}, eq, book.cash, day_start, book.peak)
                    if v["verdict"] == "REJECTED":
                        why = v["risk_note"]
                    else:
                        caps = decisions.buy_limits(sym, cd["close"], order["stop"], eq, book.cash, {}, v["qty"], fee, slip)
                        binding = min(caps, key=lambda k: caps[k][0])
                        target = decisions._unit(kind, eq * config.MAX_POSITION_PCT / cd["close"])
                        order["qty"] = decisions._unit(kind, min(target, caps[binding][0]))
                        if order["qty"] < (1 if kind == "stock" else 1e-8) or order["qty"] * cd["close"] < decisions.MIN_ORDER_USD:
                            why = f"below the minimum order after the strictest limit ({binding})"
                        decision = "NEEDS_REVIEW" if v["verdict"] == "NEEDS REVIEW" else "RISK_APPROVED"
            else:
                order["qty"] = book.pos["qty"]  # risk-reducing exit of exactly the held quantity
            if why:
                decision = "RISK_REJECTED"
                reasons[why.split(" (")[0]] += 1  # counted as rejected proposals, not as risk events
            risk_counts[decision] += 1
            order["risk_decision"] = decision
            approved = decision != "RISK_REJECTED" and c["approval_policy"] == "SIMULATED_APPROVAL"
            if approved:
                pending = order
            if len(proposals) < MAX_LISTED:
                proposals.append({"time": _iso(t1), "signal": sig, "price": cd["close"], "confidence": order["confidence"],
                                  "qty": order.get("qty"), "risk_decision": decision, "reason": why or "all checks passed",
                                  "approval": "SIMULATED_APPROVAL (backtest policy, not a human)" if approved else
                                  ("not approved: PROPOSAL_ONLY" if decision != "RISK_REJECTED" else "not sent"),
                                  "higher_timeframes": {str(h): x.at(t1) for h, x in ctx.items()}})
        prev = cd
        if run_id and i % 5000 == 0:
            _progress[run_id] = {"segment": name, "done": i, "total": n}

    if last is None:
        return {"status": "INSUFFICIENT DATA", "reason": "no confirmed candles in this segment"}
    if not chart or chart[-1]["t"] != _iso(last["close_time"]):
        chart.append({"t": _iso(last["close_time"]), "eq": round(last_eq, 2), "bh": round(bh_eq, 2), "close": last["close"],
                      "dd": round((book.peak - last_eq) / book.peak * 100, 3)})
    if pending:
        book.event(last["close_time"], "NO_FILL", side=pending["side"], why="segment ended before the next candle")
    return _summarize(book, e0, last_eq, last, i + 1, in_market, max_dd, (nr, mean, m2, down2), bpy, signals, risk_counts, reasons,
                      proposals, chart, bh_qty, bh_cost, bh_eq, bh_max_dd, gaps, missing, bad, origins, first, warm)


def _summarize(book, e0, end_eq, last, bars, in_market, max_dd, rets, bpy, signals, risk_counts, reasons, proposals, chart,
               bh_qty, bh_cost, bh_eq, bh_max_dd, gaps, missing, bad, origins, first, warm):
    """Performance Analyst on the trade list + metrics from the same equity curve. N/A where a number can't be defined."""
    na = lambda v: "N/A" if v is None else v  # noqa: E731
    trades = book.trades
    open_fee = book.pos["fee"] if book.pos else 0.0
    stats, note = performance_analyst.run([{"side": "SELL", "pnl": t["pnl"], "fee": t["fees"]} for t in trades]
                                          + ([{"side": "BUY", "pnl": None, "fee": open_fee}] if book.pos else []),
                                          [{"equity": e0}, {"equity": end_eq}], e0)
    nr, mean, m2, down2 = rets
    std = math.sqrt(m2 / (nr - 1)) if nr > 1 else 0.0
    wins, losses = [t["pnl"] for t in trades if t["pnl"] > 0], [t["pnl"] for t in trades if t["pnl"] < 0]
    pf = stats["profit_factor"]
    metrics = {
        "starting_equity": e0, "ending_equity": round(end_eq, 2), "net_profit": round(end_eq - e0, 2),
        "total_return_pct": round((end_eq / e0 - 1) * 100, 4), "completed_trades": len(trades),
        "win_rate_pct": na(stats["win_rate_pct"]),
        "profit_factor": pf if pf is not None else "N/A",
        "profit_factor_note": None if pf is not None else ("no completed trades" if not trades else "no losing trades: undefined (division by zero)"),
        "max_drawdown_pct": round(max_dd * 100, 4), "total_fees": round(sum(t["fees"] for t in trades) + open_fee, 2),
        "slippage_cost": round(sum(t["slippage_cost"] for t in trades) + (book.pos["slippage"] if book.pos else 0.0), 2),
        "rejected_proposals": risk_counts["RISK_REJECTED"], "risk_events": sum(book.event_counts.values()),
        "sharpe": round(mean / std * math.sqrt(bpy), 3) if nr >= 30 and std > 0 else "N/A",
        "sortino": round(mean / math.sqrt(down2 / nr) * math.sqrt(bpy), 3) if nr >= 30 and down2 > 0 else "N/A",
        "avg_win": round(sum(wins) / len(wins), 2) if wins else "N/A", "avg_loss": round(sum(losses) / len(losses), 2) if losses else "N/A",
        "expectancy": round(sum(t["pnl"] for t in trades) / len(trades), 2) if trades else "N/A",
        "avg_holding_hours": round(sum(t["holding_hours"] for t in trades) / len(trades), 2) if trades else "N/A",
        "exposure_time_pct": round(in_market / bars * 100, 2), "bars": bars, "performance_analyst": note,
        "open_position": None if not book.pos else {"qty": book.pos["qty"], "entry_price": book.pos["price"], "entry_time": _iso(book.pos["entry_time"]),
                                                    "unrealized_pnl": round((last["close"] - book.pos["price"]) * book.pos["qty"] - open_fee, 2)},
    }
    bench = {"name": "Buy & Hold (100% of equity, same costs)", "ending_equity": round(bh_eq, 2) if bh_qty else "N/A",
             "return_pct": round((bh_eq / e0 - 1) * 100, 4) if bh_qty else "N/A", "max_drawdown_pct": round(bh_max_dd * 100, 4),
             "trades": 1 if bh_qty else 0, "costs": round(bh_cost, 2)}
    comparison = {"strategy_return_pct": metrics["total_return_pct"], "buy_hold_return_pct": bench["return_pct"],
                  "excess_return_pct": round(metrics["total_return_pct"] - bench["return_pct"], 4) if bh_qty else "N/A",
                  "strategy_max_drawdown_pct": metrics["max_drawdown_pct"], "buy_hold_max_drawdown_pct": bench["max_drawdown_pct"],
                  "strategy_trades": len(trades), "strategy_costs": round(metrics["total_fees"] + metrics["slippage_cost"], 2),
                  "buy_hold_costs": bench["costs"]}
    return {
        "status": "COMPLETE", "from": _iso(first["open_time"]), "to": _iso(last["close_time"]), "candles": bars,
        "warmup": {"candles": len(warm), "from": _iso(warm[0]["open_time"]) if warm else None,
                   "to": _iso(warm[-1]["close_time"]) if warm else None, "used_for": "indicators only, never traded or scored"},
        "metrics": metrics, "benchmark": bench, "comparison": comparison,
        "signals": dict(signals), "risk_decisions": dict(risk_counts), "rejection_reasons": dict(reasons),
        "event_counts": dict(book.event_counts), "events": book.events, "trades": trades, "proposals": proposals, "chart": chart,
        "data_quality": {"status": "OK" if not missing and not bad else "INCOMPLETE", "missing_candles": missing, "gaps": gaps,
                         "untradable_candles": bad, "origins": {str(k): v for k, v in origins.items()}},
    }


# ---------- a whole run ----------
def run_backtest(raw, source=None, higher=None, now=None, run_id=None, store=True):
    """Validate, split 70/30 in time order, run Train and Test independently, store config + result under RUNS_DIR."""
    c = normalize(raw)
    run_id = run_id or uuid.uuid4().hex[:12]
    started = time.time()
    now = now or int(started)
    result = {"run_id": run_id, "label": "SIMULATION / HISTORICAL BACKTEST" + (" · DEMO DATA (synthetic, not real)" if c["source"] == "DEMO" else ""),
              "created_at": _iso(int(started)), "config": c, "code_version": code_version(),
              "risk_settings": {k: getattr(config, k) for k in decisions.SETTINGS_KEYS}, "notes": NOTES}
    if store:
        _write(run_id, "config.json", {k: result[k] for k in ("run_id", "created_at", "config", "code_version", "risk_settings")})
    if c["market"] == "PREDICTION":
        return _finish(result, "UNSUPPORTED", store, reason="Prediction markets have no verified historical prices, liquidity or "
                       "settlement data in this project (Lesson 8 not implemented). Nothing was simulated.")
    if source is None:
        if c["source"] == "DEMO":
            source = demo_source(step=c["interval"] * 60)
        elif c["market"] == "STOCK":
            source = stock_source(c["symbol"])
        else:
            source = SqlSource(c["symbol"].replace("-", "/"), c["interval"])
            higher = {h: SqlSource(c["symbol"].replace("-", "/"), h) for h in c["higher"]} if higher is None else higher
    higher = higher or {}
    start, end = _bounds(c)
    n, lo, hi = source.count(start, end)
    result["data"] = {**source.meta, "requested": {"start": c["start"], "end": c["end"]}, "candles": n,
                      "first_open": _iso(lo) if lo else None, "last_open": _iso(hi) if hi else None,
                      "higher_timeframes": {str(h): {"candles": s.count(start, end)[0]} for h, s in higher.items()}}
    k = int(n * c["train_ratio"])
    if k < MIN_SEGMENT or n - k < MIN_SEGMENT:
        return _finish(result, "INSUFFICIENT_DATA", store, reason=f"{n} stored candles in the requested range; Train needs {MIN_SEGMENT} "
                       f"and Test needs {MIN_SEGMENT} after a {c['train_ratio']:.0%}/{1 - c['train_ratio']:.0%} split. Nothing was simulated."
                       + (" Stock bars: run `python -m app.stockdata fetch` or import a CSV first." if c["market"] == "STOCK" else ""))
    split = source.kth(start, end, k)
    result["split"] = {"method": "time order by candle count, no shuffle; Train strictly before Test; each segment starts with a fresh "
                       "account; Test warm-up candles come from before Test and are only used for indicators",
                       "train_ratio": c["train_ratio"], "split_at": _iso(split),
                       "extensible": "segments are (start, end) pairs: Train / Validation / Test or walk-forward windows reuse _segment()"}
    for name, (a, b, m) in (("train", (lo, split - 1, k)), ("test", (split, hi, n - k))):
        result[name] = _segment(name, source, higher, a, b, m, c, now, run_id)
    q = [result[s]["data_quality"] for s in ("train", "test") if "data_quality" in result[s]]
    result["data"]["quality"] = "OK" if all(x["status"] == "OK" for x in q) else "INCOMPLETE"
    result["data"]["warnings"] = [f"{s}: {result[s]['data_quality']['missing_candles']} candle(s) missing in "
                                  f"{len(result[s]['data_quality']['gaps'])}+ gap(s); BUY signals right after a gap are rejected"
                                  for s in ("train", "test") if result[s].get("data_quality", {}).get("missing_candles")]
    result["seconds"] = round(time.time() - started, 1)
    return _finish(result, "COMPLETE", store)


def _finish(result, status, store, reason=None):
    result.update(status=status, finished_at=_iso(int(time.time())), reason=reason)
    if store:
        _write(result["run_id"], "result.json", result)
    _progress.pop(result["run_id"], None)  # only after the final file exists, so a reader never sees a false INTERRUPTED
    return result


def _write(run_id, name, obj):
    """Atomic write: a crash mid-write leaves the previous file (or none), never half a file."""
    folder = RUNS_DIR / run_id
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    os.replace(tmp, folder / name)


# ---------- background runs for the API ----------
def start(raw):
    """Validate and start ONE background run. Raises ValueError (bad settings) or RuntimeError (one already running)."""
    c = normalize(raw)
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A backtest is already running. Wait for it to finish.")
    run_id = uuid.uuid4().hex[:12]
    _progress[run_id] = {"segment": "starting", "done": 0, "total": None}
    _write(run_id, "result.json", {"run_id": run_id, "status": "RUNNING", "config": c, "created_at": _iso(int(time.time()))})

    def work():
        try:
            run_backtest(c, run_id=run_id)
        except Exception as err:  # stored, never silent
            _finish({"run_id": run_id, "config": c}, "FAILED", True, reason=f"{type(err).__name__}: {err}")
        finally:
            _lock.release()

    threading.Thread(target=work, daemon=True).start()
    return run_id


def _read(run_id):
    if not (len(run_id) == 12 and all(ch in "0123456789abcdef" for ch in run_id)):  # no path tricks
        return None
    path = RUNS_DIR / run_id / "result.json"
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if r.get("status") == "RUNNING":
        if run_id in _progress:
            r["progress"] = _progress[run_id]
        else:
            r["status"], r["reason"] = "INTERRUPTED", "the server stopped during this run; its partial result was never stored"
    return r


def get_run(run_id):
    return _read(run_id)


def list_runs(limit=30):
    if not RUNS_DIR.exists():
        return []
    out = []
    for d in sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        r = _read(d.name)
        if r:
            out.append({"run_id": r["run_id"], "status": r["status"], "created_at": r.get("created_at"), "label": r.get("label"),
                        "config": {k: r["config"].get(k) for k in ("source", "market", "symbol", "interval", "approval_policy")},
                        **{f"{s}_return_pct": r.get(s, {}).get("metrics", {}).get("total_return_pct") for s in ("train", "test")}})
    return out


def options():
    """What the dashboard can offer: markets, symbols, intervals, stored date ranges (MIN/MAX only: instant), cost assumptions."""
    ranges = {}
    try:
        with contextlib.closing(sqlite3.connect(f"file:{stockdata.DB_PATH}?mode=ro", uri=True)) as conn:
            for pair in cryptodata.PAIRS:
                for iv in MARKETS["CRYPTO_SPOT"]["intervals"]:
                    a, b = conn.execute("SELECT MIN(open_time), MAX(open_time) FROM crypto_candles WHERE exchange=? AND market_type=? "
                                        "AND pair=? AND interval_min=?", (cryptodata.EXCHANGE, cryptodata.MARKET, pair, iv)).fetchone()
                    ranges[f"{pair.replace('/', '-')}|{iv}"] = [_iso(a), _iso(b)] if a else None
            for sym in stockdata.SYMBOLS:
                a, b = conn.execute("SELECT MIN(ts), MAX(ts) FROM bars WHERE asset_class='stock' AND symbol=? AND data_type='RAW'", (sym,)).fetchone()
                ranges[f"{sym}|1440"] = [a, b] if a else None
    except sqlite3.OperationalError:
        pass
    return {"markets": MARKETS, "policies": POLICIES, "costs": config.BACKTEST_COSTS, "ranges": ranges,
            "risk_settings": {k: getattr(config, k) for k in decisions.SETTINGS_KEYS}, "label": "SIMULATION / HISTORICAL BACKTEST"}


def main(argv=None):
    p = argparse.ArgumentParser(description="Historical backtest (SIMULATION ONLY).")
    p.add_argument("cmd", choices=["run"])
    p.add_argument("--market", default="CRYPTO_SPOT")
    p.add_argument("--symbol", default="BTC-USD")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--higher", default="240,1440")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--policy", default="PROPOSAL_ONLY", choices=POLICIES)
    p.add_argument("--source", default="REAL", choices=["REAL", "DEMO"])
    a = p.parse_args(argv)
    r = run_backtest({"market": a.market, "symbol": a.symbol, "interval": a.interval, "start": a.start, "end": a.end,
                      "approval_policy": a.policy, "source": a.source, "higher": [int(h) for h in a.higher.split(",") if h]})
    print(json.dumps({"run_id": r["run_id"], "status": r["status"], "reason": r.get("reason"), "data": r.get("data"), "split": r.get("split"),
                      **{s: {"from": r[s].get("from"), "to": r[s].get("to"), "metrics": r[s].get("metrics"), "comparison": r[s].get("comparison"),
                             "signals": r[s].get("signals"), "risk_decisions": r[s].get("risk_decisions"), "event_counts": r[s].get("event_counts"),
                             "rejection_reasons": r[s].get("rejection_reasons")}
                         for s in ("train", "test") if s in r}}, indent=1, default=str))


if __name__ == "__main__":
    main()
