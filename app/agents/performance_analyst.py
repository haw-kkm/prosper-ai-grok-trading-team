"""Agent 6: Performance Analyst

ROLE:    Evaluate how the system performs so you can improve it.
INPUT:   Trade history, the equity curve, and a buy-and-hold benchmark.
OUTPUT:  Return, win rate, max drawdown, profit factor, fees paid,
         benchmark comparison and a one-line takeaway.
HANDOFF: You (shown on the dashboard).

Simulated results on demo data say nothing about real-market profits.
"""

NAME = "Performance Analyst"


def run(trades, equity_curve, starting_cash, benchmark_return_pct=None):
    closed = [t for t in trades if t["side"] == "SELL" and t["pnl"] is not None]
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [-t["pnl"] for t in closed if t["pnl"] < 0]
    fees = sum(t.get("fee", 0) for t in trades)

    peak, max_dd = None, 0.0
    for point in equity_curve:
        value = point["equity"]
        peak = value if peak is None else max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)

    equity = equity_curve[-1]["equity"] if equity_curve else starting_cash
    stats = {
        "equity": round(equity, 2),
        "total_return_pct": round((equity / starting_cash - 1) * 100, 2),
        "realized_pnl": round(sum(t["pnl"] for t in closed), 2),
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "fees_paid": round(fees, 2),
        "benchmark_return_pct": benchmark_return_pct,
    }

    if not closed:
        msg = "No closed trades yet. Results need more simulated time."
    else:
        pf = stats["profit_factor"]
        msg = (f"{len(closed)} closed trades, win rate {stats['win_rate_pct']}%, "
               f"profit factor {pf if pf is not None else 'n/a'}. "
               + ("Beating buy-and-hold so far." if benchmark_return_pct is not None
                  and stats["total_return_pct"] > benchmark_return_pct else "Not beating buy-and-hold yet."))
    return stats, msg
