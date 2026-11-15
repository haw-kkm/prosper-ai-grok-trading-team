"""Simulated (offline demo) market data for three market types.

- Stocks & ETFs:  SPY, QQQ, AAPL, NVDA, TSLA ...
- Crypto:         BTC-USD, ETH-USD, SOL-USD (moves more, trades 24/7)
- Prediction:     PM-* contracts priced 0.01-0.99 = implied probability

Prices follow a random walk with drifting trends so the dashboard behaves
like a real feed. This is NOT real market data and touches no exchange.
"""

import math
import random
from collections import deque

START_PRICES = {
    "SPY": 570.0, "QQQ": 490.0, "AAPL": 230.0, "NVDA": 135.0, "TSLA": 250.0,
    "MSFT": 420.0, "AMZN": 190.0, "GOOGL": 170.0,
    "BTC-USD": 68000.0, "ETH-USD": 2600.0, "SOL-USD": 160.0,
    "PM-FED-CUT-DEC": 0.62, "PM-BTC-100K-EOY": 0.31, "PM-RECESSION-2027": 0.24,
}

PREDICTION_LABELS = {
    "PM-FED-CUT-DEC": "Fed cuts rates in December",
    "PM-BTC-100K-EOY": "BTC above $100K by year end",
    "PM-RECESSION-2027": "US recession declared in 2027",
}

HISTORY_LENGTH = 120


def market_type(symbol: str) -> str:
    if symbol.startswith("PM-"):
        return "prediction"
    if symbol.endswith("-USD"):
        return "crypto"
    return "stock"


class SimulatedMarket:
    def __init__(self, symbols, seed=None):
        self.rng = random.Random(seed)
        self.symbols = list(symbols)
        self.prices, self.history, self.drift = {}, {}, {}
        for sym in self.symbols:
            default = 0.5 if market_type(sym) == "prediction" else self.rng.uniform(20, 300)
            start = START_PRICES.get(sym, default)
            self.prices[sym] = start
            self.history[sym] = deque([start], maxlen=HISTORY_LENGTH)
            self.drift[sym] = 0.0
        for _ in range(40):  # pre-fill history so indicators work on cycle 1
            self.tick()

    def _volatility(self, sym):
        return {"crypto": 0.012, "prediction": 0.035, "stock": 0.006}[market_type(sym)]

    def tick(self):
        for sym in self.symbols:
            self.drift[sym] = 0.9 * self.drift[sym] + self.rng.gauss(0, 0.0015)
            shock = self.drift[sym] + self.rng.gauss(0, self._volatility(sym))
            if market_type(sym) == "prediction":
                # Move in log-odds space so the price always stays between 0 and 1.
                p = self.prices[sym]
                logit = math.log(p / (1 - p)) + shock
                new_price = min(0.99, max(0.01, 1 / (1 + math.exp(-logit))))
            else:
                new_price = max(0.01, self.prices[sym] * math.exp(shock))
            self.prices[sym] = new_price
            self.history[sym].append(new_price)
        return dict(self.prices)

    def snapshot(self):
        out = {}
        for sym in self.symbols:
            hist = list(self.history[sym])
            prev = hist[-2] if len(hist) > 1 else hist[-1]
            out[sym] = {
                "type": market_type(sym),
                "label": PREDICTION_LABELS.get(sym, sym),
                "price": round(self.prices[sym], 4),
                "change_pct": round((self.prices[sym] / prev - 1) * 100, 3),
                "session_change_pct": round((self.prices[sym] / hist[0] - 1) * 100, 3),
                "history": [round(p, 4) for p in hist[-60:]],
            }
        return out
