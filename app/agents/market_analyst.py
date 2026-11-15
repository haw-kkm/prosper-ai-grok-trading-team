"""Agent 1: Market Analyst

ROLE:    Analyze price data: trend, moving averages, volatility.
INPUT:   Price history for every symbol on the watchlist.
OUTPUT:  A structured market report per symbol, including a trend score
         from -1 (strong downtrend) to +1 (strong uptrend).
HANDOFF: News Analyst + Strategy Agent.

OPTIONAL: research() asks Grok to read the same indicators. That research is
shown on the dashboard and logged, but trading decisions use run() only.

REAL DATA: analyze_bars() runs the same indicators on stored real daily bars
(app/stockdata.py). That analysis is for display/research only; it never feeds
the trading simulation.
"""

import statistics
import time

from .. import config, indicators
from ..grok import ask_grok_json

NAME = "Market Analyst"

MIN_HISTORY = 25

RESEARCH_FIELDS = ("symbol", "type", "price", "momentum_pct", "volatility_pct", "sma_fast", "sma_slow", "trend", "trend_score")
REAL_FIELDS = RESEARCH_FIELDS + ("market", "exchange", "interval", "as_of", "volume_change_pct", "ema_12", "ema_26", "rsi_14", "macd")
LIMITS = {
    "stock": "Daily RAW bars (not split-adjusted) for US sessions only. Historical, not real-time. "
             "Indicators describe the past and do not predict prices.",
    "crypto": "Prices from one exchange (Kraken spot) in a 24/7 market. Only confirmed candles, at most 720 recent "
              "ones per interval. Historical, not real-time. Indicators describe the past and do not predict prices.",
}
SIM_SOURCE = "Offline simulated prices (random walk). Not real market data."
RESEARCH_PROMPT = (
    "You are the Market Analyst on a SIMULATED, educational trading team. The user message holds indicators and a "
    "data_source field saying where the prices came from (simulated prices, or stored historical daily bars; never "
    "real-time). Analyze ONLY those numbers. "
    "Do not use outside knowledge about the tickers, do not search, and do not mention news or real-world events. "
    "For each symbol give a view (bullish, bearish or neutral), a confidence from 0 to 1, and one short sentence "
    "of rationale that cites the provided numbers. You cannot place or recommend orders; this is research only."
)
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "analyses": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "view": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
            },
            "required": ["symbol", "view", "confidence", "rationale"],
            "additionalProperties": False,
        }},
        "summary": {"type": "string"},
    },
    "required": ["analyses", "summary"],
    "additionalProperties": False,
}


def _pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def _indicators(hist):
    """SMA 5/20, 20-bar momentum, volatility of returns and a trend score from prices (oldest first).

    Uses only the values given, so callers control the cut-off date (no look-ahead). Needs MIN_HISTORY values.
    """
    returns = [_pct(hist[i], hist[i - 1]) for i in range(1, len(hist))]
    sma_fast = sum(hist[-5:]) / 5
    sma_slow = sum(hist[-20:]) / 20
    momentum = _pct(hist[-1], hist[-20])
    volatility = statistics.pstdev(returns[-20:])
    # Strong move with calm volatility = clearer trend.
    raw = momentum / max(volatility * 4, 0.5)
    if sma_fast < sma_slow:
        raw = min(raw, 0)  # fast average below slow = no uptrend
    return {
        "momentum_pct": round(momentum, 3),
        "volatility_pct": round(volatility, 3),
        "sma_fast": round(sma_fast, 4),
        "sma_slow": round(sma_slow, 4),
        "trend": "up" if sma_fast > sma_slow else "down",
        "trend_score": round(max(-1.0, min(1.0, raw)), 3),
    }


def run(market_snapshot):
    reports = []
    for sym, data in market_snapshot.items():
        hist = data["history"]
        if len(hist) < MIN_HISTORY:
            reports.append({"symbol": sym, "type": data["type"], "enough_data": False})
            continue
        reports.append({"symbol": sym, "type": data["type"], "enough_data": True, "price": data["price"], **_indicators(hist)})
    ranked = sorted((r for r in reports if r["enough_data"]), key=lambda r: abs(r["momentum_pct"]), reverse=True)
    summary = ", ".join(f"{r['symbol']} {r['momentum_pct']:+.2f}%" for r in ranked[:4]) or "not enough data yet"
    return reports, f"Biggest moves: {summary}"


def analyze_bars(symbol, bars, market="stock"):
    """The same indicators as run(), on real bars or candles (oldest first, one series). Display/research only.

    Adds volume change (latest volume vs the average of the 20 bars before it), EMA 12/26, RSI 14 and MACD.
    Returns status INSUFFICIENT DATA instead of numbers when there are fewer than MIN_HISTORY bars.
    """
    base = {"symbol": symbol, "type": market, "bars": len(bars), "as_of": bars[-1]["ts"] if bars else None}
    if len(bars) < MIN_HISTORY:
        return {**base, "enough_data": False, "status": indicators.INSUFFICIENT, "needed": MIN_HISTORY}
    closes, volumes = [b["close"] for b in bars], [b["volume"] for b in bars]
    prior = sum(volumes[-21:-1]) / 20
    return {**base, "enough_data": True, "status": "OK", "price": closes[-1], **_indicators(closes),
            "volume_change_pct": round(_pct(volumes[-1], prior), 2) if prior else None,
            "ema_12": indicators.ema(closes, 12), "ema_26": indicators.ema(closes, 26),
            "rsi_14": indicators.rsi(closes, 14), "macd": indicators.macd(closes)}


def market_report(symbol, bars, market, context):
    """Market Analyst report on real data (stock or crypto): indicators + where the data came from
    (context: exchange, interval, analysis time, source, quality, delay) + the limits of the analysis.
    Research only: it is never sent to the Strategy Agent and cannot place orders."""
    return {**analyze_bars(symbol, bars, market), **context, "market": market, "limitations": LIMITS[market]}


def research_real(analyses, data_source):
    """Interface for Grok research on REAL stock or crypto indicators from market_report() / analyze_bars().

    Not wired to any button, endpoint or timer yet, so it never runs (or costs money) on its own.
    Research only, like research(): nothing here can trade or touch risk limits.
    """
    return research(analyses, data_source=data_source, fields=REAL_FIELDS)


def research(reports, data_source=SIM_SOURCE, fields=RESEARCH_FIELDS):
    """Ask Grok to read the indicators from run(). Returns a research record, or None if Grok is off or failed.

    The record keeps the exact data sent (`inputs`) next to Grok's views and rationale.
    """
    rows = [{k: r.get(k) for k in fields} for r in reports if r.get("enough_data")]
    if not rows:
        return None
    payload = {"data_source": data_source, "symbols": rows}
    sent = {r["symbol"] for r in rows}

    def validate(data):
        analyses = {}
        for a in data.get("analyses", []):
            sym, view = a.get("symbol"), a.get("view")
            if sym in sent and view in ("bullish", "bearish", "neutral"):  # ignore symbols we never sent
                analyses[sym] = {"view": view, "confidence": round(min(1.0, max(0.0, float(a.get("confidence", 0)))), 2),
                                 "rationale": str(a.get("rationale", ""))[:300]}
        if not analyses:
            raise ValueError("no usable analysis for the symbols that were sent")
        return {"analyses": analyses, "summary": str(data.get("summary", ""))[:300]}

    result = ask_grok_json(RESEARCH_PROMPT, payload, "market_research", RESEARCH_SCHEMA, validate)
    if result is None:
        return None
    return {"time": time.time(), "model": config.XAI_MODEL, "inputs": payload, **result}
