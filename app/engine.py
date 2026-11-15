"""The trading floor: runs all 6 agents in order, every cycle.

MARKET DATA (offline demo)
  -> Market Analyst + News Analyst
  -> Strategy Agent
  -> Risk Manager
  -> Human approval (optional, toggle on the dashboard)
  -> Portfolio Manager (simulated execution)
  -> Performance Analyst
  -> Dashboard

Grok (optional) only runs in grok_test() / grok_research(), on request. It returns
research; it cannot trade, approve, size orders or change risk limits.
"""

import json
import logging
import threading
import time
import uuid
from collections import deque

from . import config, grok
from .agents import TEAM, market_analyst, news_analyst, performance_analyst, portfolio_manager, risk_manager, strategy_agent
from .broker import PaperBroker
from .market import SimulatedMarket

log = logging.getLogger("engine")

PENDING_EXPIRES_SECONDS = 300  # unapproved proposals expire after 5 minutes
GROK_MIN_GAP_SECONDS = 15      # at most one Grok action every 15s, however often a button is pressed
GROK_AUTO_MAX_FAILURES = 3     # opt-in auto research stops after 3 failures in a row (no endless retries)
NEWS_SCORES_TTL_SECONDS = 900  # Grok news scores older than 15 min stop feeding the Strategy Agent
RESEARCH_LOG = config.DATA_DIR / "grok_research.jsonl"


class TradingFloor:
    def __init__(self, seed=None):
        self.market = SimulatedMarket(config.WATCHLIST, seed=seed)
        self.broker = PaperBroker(config.STARTING_CASH)
        self.feed = deque(maxlen=80)
        self.risk_log = deque(maxlen=50)
        self.equity_curve = deque(maxlen=300)
        self.agent_status = {}
        self.latest_proposals = []
        self.pending = {}  # id -> verdict waiting for human approval
        self.stats = {}
        self.cycle = 0
        self.running = True
        self.require_approval = config.REQUIRE_APPROVAL
        self.peak_equity = self.broker.starting_cash
        self._benchmark_start = dict(self.market.prices)
        self._last_grok = 0.0
        self._news_scores = {}
        self._news_msg = ""
        self._last_logged = {}
        self.agent_runs = {}         # agent name -> time its step last finished (dashboard only)
        self.last_error = None       # {"time", "agent", "error"} of the most recent failed step
        self.market_updated = None   # time of the last simulated price tick
        self.latest_reports = []     # Market Analyst output from the last cycle
        self.ai_research = None      # latest validated Grok market research (display only)
        self._news_time = None       # when Grok last scored provided news
        self._grok_busy = threading.Lock()
        self._grok_failures = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ---------- helpers ----------
    def _say(self, agent, message):
        with self._lock:
            self.feed.appendleft({"time": time.time(), "agent": agent, "message": message})
            self.agent_status[agent] = message

    def _run(self, name, fn, *args, **kwargs):
        """Call one agent step and record when it finished, or what broke, for the dashboard."""
        try:
            out = fn(*args, **kwargs)
        except Exception as err:
            self.last_error = {"time": time.time(), "agent": name, "error": grok.redact(f"{type(err).__name__}: {err}")}
            raise
        self.agent_runs[name] = time.time()
        return out

    def _prices(self):
        return dict(self.market.prices)

    def _benchmark_return(self):
        """Equal-weight buy-and-hold of the whole watchlist since start."""
        rets = [self.market.prices[s] / p - 1 for s, p in self._benchmark_start.items() if p]
        return round(sum(rets) / len(rets) * 100, 2) if rets else None

    # ---------- one full cycle ----------
    def run_cycle(self):
        with self._lock:
            self.cycle += 1
            self.market.tick()
            self.market_updated = time.time()
            snap = self.market.snapshot()
            prices = self._prices()

            reports, msg = self._run(market_analyst.NAME, market_analyst.run, snap)
            self.latest_reports = reports
            self._say(market_analyst.NAME, msg)

            # Grok news scores only come from grok_research(). Drop them once stale so old AI input can't drive trades.
            if self._news_time and time.time() - self._news_time > NEWS_SCORES_TTL_SECONDS:
                self._news_scores, self._news_msg, self._news_time = {}, "", None
            if not self._news_msg:
                self._news_scores, self._news_msg = self._run(
                    news_analyst.NAME, news_analyst.run, reports, news_items=news_analyst.load_news())
                self._say(news_analyst.NAME, self._news_msg)

            actionable, all_proposals, msg = self._run(
                strategy_agent.NAME, strategy_agent.run, reports, self._news_scores, self.broker.positions,
                ai_research=(self.ai_research or {}).get("analyses"))
            self.latest_proposals = all_proposals
            self._say(strategy_agent.NAME, msg)

            exits = self._run(risk_manager.NAME, risk_manager.check_exits, self.broker.positions, prices)
            equity = self.broker.equity(prices)
            self.peak_equity = max(self.peak_equity, equity)
            already_pending = {p["symbol"] for p in self.pending.values()}
            to_review = [p for p in actionable if p["symbol"] not in already_pending] if self.running else []
            verdicts, msg = self._run(
                risk_manager.NAME, risk_manager.review,
                to_review, self.broker.positions, equity, self.broker.cash,
                self.broker.day_start_equity, self.peak_equity,
            )
            if exits:
                msg = "; ".join(f"{e[2]} on {e[0]}" for e in exits) + (f" | {msg}" if msg else "")
            self._say(risk_manager.NAME, msg)

            orders = []
            for v in verdicts:
                key = (v["symbol"], v["verdict"], v["risk_note"])
                if time.time() - self._last_logged.get(key, 0) > 60:  # don't repeat the same verdict every cycle
                    self._last_logged[key] = time.time()
                    self.risk_log.appendleft({"time": time.time(), **{k: v.get(k) for k in ("symbol", "action", "verdict", "risk_note", "confidence")}})
                if v["verdict"] == "REJECTED":
                    continue
                needs_human = self.require_approval or v["verdict"] == "NEEDS REVIEW"
                if needs_human and v["action"] == "BUY":
                    pid = uuid.uuid4().hex[:8]
                    self.pending[pid] = {**v, "id": pid, "created": time.time()}
                else:
                    orders.append(v)

            # Expire stale proposals: the setup has probably changed.
            now = time.time()
            for pid in [k for k, p in self.pending.items() if now - p["created"] > PENDING_EXPIRES_SECONDS]:
                self.pending.pop(pid)

            fills, msg = self._run(portfolio_manager.NAME, portfolio_manager.run, self.broker, orders, exits)
            if fills or self.cycle % 3 == 0:
                self._say(portfolio_manager.NAME, msg + (f" {len(self.pending)} waiting for your approval." if self.pending else ""))
            if fills:
                self.broker.save()

            equity = self.broker.equity(prices)
            self.equity_curve.append({"time": time.time(), "equity": round(equity, 2)})
            self.stats, msg = self._run(
                performance_analyst.NAME, performance_analyst.run,
                self.broker.trades, list(self.equity_curve), self.broker.starting_cash, self._benchmark_return(),
            )
            if self.cycle % 6 == 0 or fills:
                self._say(performance_analyst.NAME, msg)

    # ---------- human approval ----------
    def decide(self, pid, approve):
        with self._lock:
            p = self.pending.pop(pid, None)
            if not p:
                return False, "That proposal expired or was already handled."
            if not approve:
                self._say("You", f"Rejected BUY {p['symbol']}.")
                return True, "Rejected."
            price = self.market.prices[p["symbol"]]  # fill at the CURRENT price
            order = {**p, "price": price, "reason": p["reason"] + " (human approved)"}
            fills, _ = self._run(portfolio_manager.NAME, portfolio_manager.run, self.broker, [order], [])
            if not fills:
                return False, "Not enough simulated cash anymore."
            self.broker.save()
            self._say("You", f"Approved BUY {p['symbol']} at {price:,.4f}.")
            return True, "Approved and filled (simulated)."

    # ---------- Grok (optional, research only) ----------
    def _grok_guard(self):
        """Why a Grok request can't run right now, or None if it can. Caller must then take _grok_busy."""
        if not config.grok_enabled():
            return "Grok is off (no XAI_API_KEY). Running as an offline demo."
        wait = GROK_MIN_GAP_SECONDS - (time.time() - self._last_grok)
        return f"Please wait {wait:.0f}s before the next Grok request." if wait > 0 else None

    def grok_test(self):
        """ONE connection check (key + model, no text generated). Never trades."""
        problem = self._grok_guard()
        if problem:
            return False, problem
        if not self._grok_busy.acquire(blocking=False):
            return False, "A Grok request is already running."
        try:
            self._last_grok = time.time()
            st = grok.test_connection()
        finally:
            self._grok_busy.release()
        if st["state"] == "CONNECTED":
            return True, f"Connected. Model {st['model']} is available."
        err = st["last_error"] or {}
        return False, f"Connection failed ({err.get('kind')}): {err.get('message')}"

    def grok_research(self):
        """One research pass: Market Analyst + News Analyst ask Grok about the latest data.

        Research only. Nothing here places, sizes or approves a trade or touches a risk limit.
        Grok news scores feed the Strategy Agent's existing rule; every proposal still goes
        through the Risk Manager and, if on, human approval.
        """
        problem = self._grok_guard()
        if problem:
            return False, problem
        if not self._grok_busy.acquire(blocking=False):
            return False, "A Grok request is already running."
        try:
            self._last_grok = time.time()
            with self._lock:
                reports = [dict(r) for r in self.latest_reports]
            if not reports:
                return False, "No market data yet. Try again after the first cycle."
            failures = grok.status()["failures"]
            items = news_analyst.load_news()
            # Network calls happen outside the trading lock so the simulation keeps running.
            research = self._run(market_analyst.NAME, market_analyst.research, reports)
            scores, note = self._run(news_analyst.NAME, news_analyst.run, reports, use_grok=True, news_items=items)
            ok = grok.status()["failures"] == failures
            with self._lock:
                if research:
                    self.ai_research = research
                    self._say(market_analyst.NAME, "Grok research (opinion on simulated data, not used for trades): " + research["summary"])
                self._news_scores, self._news_msg, self._news_time = scores, note, time.time()
                self._say(news_analyst.NAME, note)
            self._grok_failures = 0 if ok else self._grok_failures + 1
            self._log_research(ok, research, items, note)
        finally:
            self._grok_busy.release()
        if ok:
            return True, "Grok research finished."
        err = grok.status()["last_error"] or {}
        return False, f"Grok research failed ({err.get('kind')}): {err.get('message')}"

    def _log_research(self, ok, research, news_items, news_note):
        """Append what was sent to Grok and what came back to data/grok_research.jsonl. Never the API key."""
        st = grok.status()
        entry = {"time": time.time(), "model": config.XAI_MODEL, "response_model": st["last_response_model"], "ok": ok,
                 "usage": st["last_usage"] if ok else None, "market_research": research,
                 "news_items": news_items, "news_note": news_note, "error": None if ok else st["last_error"]}
        try:
            with RESEARCH_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            log.warning("Could not write %s", RESEARCH_LOG.name)

    # ---------- lifecycle ----------
    def loop(self):
        log.info("Trading floor open. SIMULATION ONLY. Cycle every %ss.", config.CYCLE_SECONDS)
        while not self._stop.is_set():
            started = time.time()
            try:
                self.run_cycle()
            except Exception as err:
                log.exception("Cycle failed. Continuing next cycle.")
                if not self.last_error or self.last_error["time"] < started:  # not already pinned on an agent
                    self.last_error = {"time": time.time(), "agent": "Engine", "error": grok.redact(f"{type(err).__name__}: {err}")}
            # Opt-in only (GROK_AUTO_RESEARCH_SECONDS > 0). Stops after repeated failures until a manual run succeeds.
            if (config.GROK_AUTO_RESEARCH_SECONDS and self._grok_failures < GROK_AUTO_MAX_FAILURES
                    and time.time() - self._last_grok >= config.GROK_AUTO_RESEARCH_SECONDS):
                try:
                    self.grok_research()
                except Exception:
                    log.exception("Automatic Grok research failed.")
            self._stop.wait(config.CYCLE_SECONDS)

    def stop(self):
        self._stop.set()
        self.broker.save()

    def reset(self):
        with self._lock:
            self.broker.reset()
            self.equity_curve.clear()
            self.pending.clear()
            self.risk_log.clear()
            self.peak_equity = self.broker.starting_cash
            self._benchmark_start = dict(self.market.prices)
            self._say("System", "Simulated account reset to starting cash.")

    # ---------- dashboard payload ----------
    def state(self):
        with self._lock:
            snap = self.market.snapshot()
            prices = self._prices()
            positions = []
            for sym, pos in self.broker.positions.items():
                price = prices.get(sym, pos["avg_price"])
                positions.append({
                    "symbol": sym, "type": snap.get(sym, {}).get("type"),
                    "qty": round(pos["qty"], 6), "avg_price": round(pos["avg_price"], 4), "price": price,
                    "value": round(pos["qty"] * price, 2),
                    "pnl": round((price - pos["avg_price"]) * pos["qty"], 2),
                    "pnl_pct": round((price / pos["avg_price"] - 1) * 100, 2),
                })
            equity = self.broker.equity(prices)
            day_start, peak = self.broker.day_start_equity, self.peak_equity
            day_change = equity / day_start - 1 if day_start else 0
            drawdown = 1 - equity / peak if peak else 0
            return {
                "server_time": time.time(),
                "cycle_seconds": config.CYCLE_SECONDS,
                "grok_status": grok.status(),
                "grok_auto_seconds": config.GROK_AUTO_RESEARCH_SECONDS,
                "ai_research": self.ai_research,
                "starting_cash": self.broker.starting_cash,
                "trade_count": len(self.broker.trades),
                "market_updated": self.market_updated,
                "reports": self.latest_reports,
                "news_scores": self._news_scores,
                "last_error": self.last_error,
                # Display only. Same comparisons as risk_manager.review (lines 42-48); the Risk Manager still decides.
                "risk_status": {
                    "equity": round(equity, 2),
                    "day_start_equity": round(day_start, 2),
                    "day_change_pct": round(day_change * 100, 2),
                    "peak_equity": round(peak, 2),
                    "drawdown_pct": round(drawdown * 100, 2),
                    "daily_loss_halt": day_change <= -config.MAX_DAILY_LOSS_PCT,
                    "drawdown_halt": drawdown >= config.MAX_DRAWDOWN_PCT,
                    "open_positions": len(self.broker.positions),
                    "crypto_exposure_pct": round(risk_manager._exposure(self.broker.positions, "crypto") / equity * 100, 2) if equity else None,
                },
                "mode": "SIMULATION",
                "data_source": "offline demo data",
                "grok": config.grok_enabled(),
                "running": self.running,
                "require_approval": self.require_approval,
                "cycle": self.cycle,
                "cash": round(self.broker.cash, 2),
                "stats": self.stats,
                "market": snap,
                "positions": positions,
                "proposals": self.latest_proposals,
                "pending": sorted(self.pending.values(), key=lambda p: -p["created"]),
                "risk_log": list(self.risk_log)[:25],
                "trades": list(reversed(self.broker.trades[-25:])),
                "feed": list(self.feed)[:40],
                "agents": [{"name": a.NAME, "status": self.agent_status.get(a.NAME, "Starting up..."),
                            "last_run": self.agent_runs.get(a.NAME)} for a in TEAM],
                "equity_curve": list(self.equity_curve),
                "risk": {
                    "max_position_pct": config.MAX_POSITION_PCT,
                    "max_open_positions": config.MAX_OPEN_POSITIONS,
                    "stop_loss_pct": config.STOP_LOSS_PCT,
                    "take_profit_pct": config.TAKE_PROFIT_PCT,
                    "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                    "max_drawdown_pct": config.MAX_DRAWDOWN_PCT,
                    "max_crypto_exposure_pct": config.MAX_CRYPTO_EXPOSURE_PCT,
                    "fee_pct": config.FEE_PCT,
                    "review_below_confidence": config.REVIEW_BELOW_CONFIDENCE,
                },
            }
