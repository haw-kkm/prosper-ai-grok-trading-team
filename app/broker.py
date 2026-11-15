"""Simulated (paper) broker. Tracks fake cash, positions and trades, with a
small simulated fee per trade so results are a bit more realistic.

There is intentionally NO code in this project that can place a real order.
"""

import json
import threading
import time

from . import config

STATE_FILE = config.DATA_DIR / "paper_account.json"


class PaperBroker:
    def __init__(self, starting_cash: float):
        self.lock = threading.Lock()
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions = {}  # symbol -> {"qty", "avg_price", "opened_at"}
        self.trades = []
        self.day_start_equity = starting_cash
        self._load()

    # ---------- persistence ----------
    def _load(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.cash = float(data["cash"])
            self.positions = data.get("positions", {})
            self.trades = data.get("trades", [])[-500:]
            self.starting_cash = float(data.get("starting_cash", self.starting_cash))
        except (ValueError, KeyError, json.JSONDecodeError):
            pass  # corrupted file: start fresh rather than crash

    def save(self):
        with self.lock:
            payload = {
                "starting_cash": self.starting_cash,
                "cash": self.cash,
                "positions": self.positions,
                "trades": self.trades[-500:],
            }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def reset(self):
        with self.lock:
            self.cash = self.starting_cash
            self.positions = {}
            self.trades = []
            self.day_start_equity = self.starting_cash
        self.save()

    # ---------- account math ----------
    def equity(self, prices):
        with self.lock:
            value = self.cash
            for sym, pos in self.positions.items():
                value += pos["qty"] * prices.get(sym, pos["avg_price"])
            return value

    # ---------- orders (simulated fills at current price) ----------
    def buy(self, symbol, qty, price, reason, agent="Portfolio Manager"):
        fee = qty * price * config.FEE_PCT
        cost = qty * price
        with self.lock:
            if qty <= 0 or cost + fee > self.cash:
                return None
            self.cash -= cost + fee
            pos = self.positions.get(symbol)
            if pos:
                total = pos["qty"] + qty
                pos["avg_price"] = (pos["avg_price"] * pos["qty"] + cost) / total
                pos["qty"] = total
            else:
                self.positions[symbol] = {"qty": qty, "avg_price": price, "opened_at": time.time()}
            trade = self._record("BUY", symbol, qty, price, reason, agent, pnl=None, fee=fee)
        return trade

    def sell(self, symbol, price, reason, agent="Risk Manager"):
        with self.lock:
            pos = self.positions.pop(symbol, None)
            if not pos:
                return None
            qty = pos["qty"]
            fee = qty * price * config.FEE_PCT
            self.cash += qty * price - fee
            pnl = (price - pos["avg_price"]) * qty - fee
            trade = self._record("SELL", symbol, qty, price, reason, agent, pnl=pnl, fee=fee)
        return trade

    def _record(self, side, symbol, qty, price, reason, agent, pnl, fee=0.0):
        trade = {
            "time": time.time(),
            "side": side,
            "symbol": symbol,
            "qty": round(qty, 6),
            "price": round(price, 4),
            "reason": reason,
            "agent": agent,
            "pnl": None if pnl is None else round(pnl, 2),
            "fee": round(fee, 2),
        }
        self.trades.append(trade)
        return trade
