"""Agent 2: News Analyst

ROLE:    Add news / sentiment context to the market report.
INPUT:   Market Analyst reports + news items YOU provide in data/news.json.
TOOLS:   Grok (only if XAI_API_KEY is set, and only when research is requested).
OUTPUT:  A sentiment adjustment per symbol from -1 to +1, plus a note
         saying where the information came from.
HANDOFF: Strategy Agent.

RULE: This project has NO news feed of its own. Without provided news items the
agent says news is unavailable and adds zero adjustment; it never asks Grok to
recall or invent news. With items, Grok may only score symbols it can tie to a
provided item. Provided items are labeled as not verified by this system.

data/news.json format (a list, max 30 items):
    [{"symbol": "AAPL", "headline": "...", "source": "Reuters", "published": "2026-11-15"}]
"""

import json
import logging

from .. import config
from ..grok import ask_grok_json

NAME = "News Analyst"
NEWS_FILE = config.DATA_DIR / "news.json"
MAX_ITEMS = 30

log = logging.getLogger("news")

SYSTEM_PROMPT = (
    "You are the News Analyst on a SIMULATED, educational trading team. The user message holds news items that "
    "the user supplied; they have NOT been verified. Score ONLY from those items. Do not use outside knowledge, "
    "do not search, and never invent or recall other news. For each symbol you score, list the ids of the items "
    "the score is based on. Skip symbols that have no relevant item. Scores go from -1 (very negative) to 1 "
    "(very positive). You cannot place or recommend orders; this is research only."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "score": {"type": "number", "minimum": -1, "maximum": 1},
                "item_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["symbol", "score", "item_ids"],
            "additionalProperties": False,
        }},
        "note": {"type": "string"},
    },
    "required": ["scores", "note"],
    "additionalProperties": False,
}


def load_news():
    """News items from data/news.json. Invalid entries are skipped; a broken file means no news."""
    if not NEWS_FILE.exists():
        return []
    try:
        raw = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("data/news.json is not valid JSON; treating it as no news.")
        return []
    items = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict) and all(isinstance(it.get(k), str) and it[k].strip() for k in ("symbol", "headline", "source")):
            items.append({"symbol": it["symbol"].strip().upper(), "headline": it["headline"].strip()[:300],
                          "source": it["source"].strip()[:100], "published": str(it.get("published", ""))[:40]})
    return items[:MAX_ITEMS]


def run(market_reports, use_grok=False, news_items=None):
    symbols = [r["symbol"] for r in market_reports]
    zeros = {sym: 0.0 for sym in symbols}
    items = [it for it in (news_items or []) if it["symbol"] in symbols]
    if not items:
        return zeros, ("No news data provided (no verified feed, nothing in data/news.json). "
                       "News sentiment unavailable; using market data only.")
    if not use_grok:
        return zeros, (f"{len(items)} provided news item(s) not analyzed yet (Grok research not run). "
                       "News sentiment unavailable; using market data only.")

    indexed = [{"id": i, **it} for i, it in enumerate(items)]

    def validate(data):
        scores = {}
        for s in data.get("scores", []):
            sym = s.get("symbol")
            cited = [i for i in s.get("item_ids", []) if isinstance(i, int) and 0 <= i < len(indexed) and indexed[i]["symbol"] == sym]
            if sym in zeros and cited:  # a score must rest on a provided item about that symbol
                scores[sym] = round(max(-1.0, min(1.0, float(s.get("score", 0)))), 3)
        return {"scores": scores, "note": str(data.get("note", ""))[:200]}

    result = ask_grok_json(SYSTEM_PROMPT, {"news_items": indexed}, "news_sentiment", SCHEMA, validate)
    if result is None:
        return zeros, "Grok news analysis failed. News sentiment unavailable; using market data only."
    return {**zeros, **result["scores"]}, (
        f"Grok sentiment on {len(items)} provided news item(s) (opinion; items not verified by this system): {result['note']}")
