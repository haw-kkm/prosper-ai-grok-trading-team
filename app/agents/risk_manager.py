"""Agent 4: Risk Manager

ROLE:    Protect the simulated portfolio. Every proposal gets a verdict:
         APPROVED, REJECTED or NEEDS REVIEW. Also forces stop-loss and
         take-profit exits.
INPUT:   Strategy proposals, open positions, prices, account equity.
OUTPUT:  Verdicts (approved orders are sized) + forced exits.
HANDOFF: Human approval (if turned on), then Portfolio Manager.

These are deterministic rules in code, not AI guesses. Change them in .env.
Risk rules reduce risk; they do not eliminate it.
"""

from .. import config
from ..market import market_type

NAME = "Risk Manager"


def check_exits(positions, prices):
    exits = []
    for sym, pos in positions.items():
        price = prices.get(sym)
        if price is None:
            continue
        change = price / pos["avg_price"] - 1
        if change <= -config.STOP_LOSS_PCT:
            exits.append((sym, price, f"Stop-loss hit ({change * 100:.1f}%)"))
        elif change >= config.TAKE_PROFIT_PCT:
            exits.append((sym, price, f"Take-profit hit (+{change * 100:.1f}%)"))
    return exits


def _exposure(positions, kind):
    return sum(p["qty"] * p["avg_price"] for s, p in positions.items() if market_type(s) == kind)


def review(proposals, positions, equity, cash, day_start_equity, peak_equity=None):
    """Return (verdicts, message). Each verdict is the proposal plus 'verdict' and 'risk_note'."""
    verdicts = []

    day_change = equity / day_start_equity - 1 if day_start_equity else 0
    drawdown = 1 - equity / peak_equity if peak_equity else 0
    halt = None
    if day_change <= -config.MAX_DAILY_LOSS_PCT:
        halt = f"Daily loss limit hit ({day_change * 100:.1f}%)"
    elif drawdown >= config.MAX_DRAWDOWN_PCT:
        halt = f"Max drawdown hit ({drawdown * 100:.1f}%)"

    slots = config.MAX_OPEN_POSITIONS - len(positions)
    for p in proposals:
        if p["action"] == "SELL":
            # Closing risk is always allowed.
            verdicts.append({**p, "verdict": "APPROVED", "risk_note": "Exits are always allowed"})
            continue
        if halt:
            verdicts.append({**p, "verdict": "REJECTED", "risk_note": f"{halt}. New trades paused."})
            continue
        if slots <= 0:
            verdicts.append({**p, "verdict": "REJECTED", "risk_note": f"Max {config.MAX_OPEN_POSITIONS} open positions"})
            continue

        kind = market_type(p["symbol"])
        pct_limit = config.MAX_POSITION_PCT * (0.5 if kind == "prediction" else 1.0)
        budget = min(equity * pct_limit, cash * 0.95)
        if kind == "crypto":
            room = equity * config.MAX_CRYPTO_EXPOSURE_PCT - _exposure(positions, "crypto")
            budget = min(budget, room)
        if budget < 50:
            verdicts.append({**p, "verdict": "REJECTED", "risk_note": "Not enough cash or exposure room"})
            continue

        qty = budget / p["price"]
        if kind == "stock":
            qty = int(qty)  # whole shares only
        if qty <= 0:
            verdicts.append({**p, "verdict": "REJECTED", "risk_note": "Price too high for position limit"})
            continue

        verdict = "NEEDS REVIEW" if p["confidence"] < config.REVIEW_BELOW_CONFIDENCE else "APPROVED"
        verdicts.append({**p, "qty": qty, "verdict": verdict,
                         "risk_note": f"Sized to {pct_limit * 100:.1f}% of equity max"})
        cash -= qty * p["price"]
        slots -= 1

    if not verdicts:
        return [], halt + ". New trades paused." if halt else "Nothing to review."
    counts = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    return verdicts, ", ".join(f"{n} {k}" for k, n in counts.items())
