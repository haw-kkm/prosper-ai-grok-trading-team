"""Agent 5: Portfolio Manager

ROLE:    Run the simulated account: execute approved trades on the PAPER
         broker, track cash, positions, exposure and P&L.
INPUT:   Approved verdicts (after human approval if it's on) + forced exits.
OUTPUT:  Simulated fills and an updated portfolio.
HANDOFF: Performance Analyst.

No real-money trades are ever executed.
"""

NAME = "Portfolio Manager"


def run(broker, orders, exits):
    fills = []
    for sym, price, reason in exits:
        trade = broker.sell(sym, price, reason, agent="Risk Manager")
        if trade:
            fills.append(trade)
    for o in orders:
        if o["action"] == "SELL":
            trade = broker.sell(o["symbol"], o["price"], o["reason"], agent=NAME)
        else:
            trade = broker.buy(o["symbol"], o["qty"], o["price"], o["reason"], agent=NAME)
        if trade:
            fills.append(trade)
    msg = ("Simulated fills: " + ", ".join(f"{t['side']} {t['symbol']}" for t in fills)) if fills else "No fills this cycle."
    return fills, msg
