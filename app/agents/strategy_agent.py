"""Agent 3: Strategy Agent

ROLE:    Combine market + news analysis into trading proposals.
INPUT:   Market Analyst reports, News Analyst sentiment, open positions.
OUTPUT:  One decision per symbol: BUY, SELL, HOLD or NO TRADE, with a
         confidence score and the reasoning behind it.
HANDOFF: Risk Manager (who can reject any proposal).

A proposal is a hypothesis, not proof a trade will be profitable.

The decision uses only the rule inputs (trend score + News Analyst score). Grok's
market research, if any, is attached as `ai_research` for display and never
changes the action.
"""

NAME = "Strategy Agent"

BUY_THRESHOLD = 0.45    # combined score needed to propose a BUY
SELL_THRESHOLD = -0.45  # combined score that triggers a SELL on a held position
NEWS_WEIGHT = 0.3       # how much the News Analyst can move the score


def run(market_reports, news_scores, open_positions, ai_research=None):
    proposals = []
    for r in market_reports:
        sym = r["symbol"]
        if not r.get("enough_data"):
            proposals.append({"symbol": sym, "action": "NO TRADE", "confidence": 0, "reason": "Not enough data"})
            continue
        score = (1 - NEWS_WEIGHT) * r["trend_score"] + NEWS_WEIGHT * news_scores.get(sym, 0.0)
        if news_scores.get(sym, 0.0) == 0.0:
            score = r["trend_score"]  # no news input: rely on market data alone
        held = sym in open_positions
        if held and score <= SELL_THRESHOLD:
            action, why = "SELL", f"Trend turned down (score {score:.2f})"
        elif held:
            action, why = "HOLD", f"Position open, trend score {score:.2f}"
        elif score >= BUY_THRESHOLD:
            action, why = "BUY", f"{r['trend'].title()}trend, momentum {r['momentum_pct']:+.2f}%, score {score:.2f}"
        else:
            action, why = "NO TRADE", f"Setup not strong enough (score {score:.2f})"
        proposals.append(
            {"symbol": sym, "type": r["type"], "action": action, "price": r["price"],
             "confidence": round(abs(score), 2), "reason": why,
             "inputs": {"trend_score": r["trend_score"], "news_score": news_scores.get(sym, 0.0)},
             "ai_research": (ai_research or {}).get(sym)}  # reference only, not used above
        )

    actionable = [p for p in proposals if p["action"] in ("BUY", "SELL")]
    actionable.sort(key=lambda p: p["confidence"], reverse=True)
    if actionable:
        msg = "Proposals: " + ", ".join(f"{p['action']} {p['symbol']} ({p['confidence']})" for p in actionable[:3])
    else:
        msg = "No strong setups. NO TRADE / HOLD across the board."
    return actionable[:4], proposals, msg
