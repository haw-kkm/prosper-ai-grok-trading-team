"""Settings for the Grok AI Trading Team (offline demo, simulation only).

Reads a local `.env` file if one exists. No extra packages needed.
Every setting has a safe default, so the project runs with no setup at all.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Grok (xAI) is optional and research-only. Without a key the team uses built-in rule-based logic.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.3").strip()  # a model id listed at docs.x.ai/developers/models
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")
# Grok is only called when you press a dashboard button. Set this to opt in to automatic
# research every N seconds (minimum 300). 0 = off (default) so nothing costs money by surprise.
_auto = _get_int("GROK_AUTO_RESEARCH_SECONDS", 0)
GROK_AUTO_RESEARCH_SECONDS = max(300, _auto) if _auto > 0 else 0

# Real US stock daily bars (Alpha Vantage, free tier). Optional: without a key, import a CSV instead.
# Only used by `python -m app.stockdata ...` commands you run yourself; the server never calls it.
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()

# Paper trading only. There is no live-trading code in this project.
STARTING_CASH = _get_float("STARTING_CASH", 100_000.0)
WATCHLIST = [
    s.strip().upper()
    for s in os.environ.get("WATCHLIST", "SPY,QQQ,AAPL,NVDA,TSLA,BTC-USD,ETH-USD,SOL-USD,PM-FED-CUT-DEC,PM-BTC-100K-EOY").split(",")
    if s.strip()
]

# Risk rules the Risk Manager enforces on every trade.
MAX_POSITION_PCT = _get_float("MAX_POSITION_PCT", 0.15)   # max 15% of equity per position
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 5)
STOP_LOSS_PCT = _get_float("STOP_LOSS_PCT", 0.05)         # exit if down 5%
TAKE_PROFIT_PCT = _get_float("TAKE_PROFIT_PCT", 0.10)     # exit if up 10%
MAX_DAILY_LOSS_PCT = _get_float("MAX_DAILY_LOSS_PCT", 0.03)  # pause new trades after -3% day
MAX_DRAWDOWN_PCT = _get_float("MAX_DRAWDOWN_PCT", 0.10)      # pause new trades after -10% from peak
MAX_CRYPTO_EXPOSURE_PCT = _get_float("MAX_CRYPTO_EXPOSURE_PCT", 0.30)  # max 30% of equity in crypto
REVIEW_BELOW_CONFIDENCE = _get_float("REVIEW_BELOW_CONFIDENCE", 0.55)  # weaker setups = NEEDS REVIEW
FEE_PCT = _get_float("FEE_PCT", 0.001)                       # simulated 0.1% cost per trade

# Lesson 10 additions (decision engine, app/decisions.py). None of these loosens a limit above.
# Total cap defaults to the value Lesson 9 enforced: max open positions x max position size (5 x 15% = 75%).
MAX_TOTAL_EXPOSURE_PCT = _get_float("MAX_TOTAL_EXPOSURE_PCT", MAX_OPEN_POSITIONS * MAX_POSITION_PCT)
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 0.01)  # planned loss at the stop <= 1% of equity
SLIPPAGE_PCT = _get_float("SLIPPAGE_PCT", 0.001)             # assumed 0.1% worse fill on entries and stop exits
MAX_CONSECUTIVE_LOSSES = _get_int("MAX_CONSECUTIVE_LOSSES", 3)  # closed losing trades in a row -> cooldown
COOLDOWN_HOURS = _get_float("COOLDOWN_HOURS", 24)            # no new BUYs for this long after a losing streak

# Lesson 11 backtest cost ASSUMPTIONS per market (app/backtest.py). None of these is a verified quote: check your own
# venue before trusting a result. Each run can override fee/slippage; the values used are stored with the run.
BACKTEST_COSTS = {
    "CRYPTO_SPOT": {"fee_pct": _get_float("BACKTEST_CRYPTO_FEE_PCT", 0.004),
                    "slippage_pct": _get_float("BACKTEST_CRYPTO_SLIPPAGE_PCT", 0.001),
                    "source": "ASSUMPTION: Kraken Pro spot taker fee for the lowest volume tier as published in 2024 (0.40%). "
                              "Not verified by this project; fees change, check kraken.com before relying on it. "
                              "Slippage 0.10% = this project's conservative SLIPPAGE_PCT, not measured."},
    "STOCK": {"fee_pct": _get_float("BACKTEST_STOCK_FEE_PCT", 0.001),
              "slippage_pct": _get_float("BACKTEST_STOCK_SLIPPAGE_PCT", 0.001),
              "source": "PLACEHOLDER: this project's simulated FEE_PCT (0.10%), not a broker quote (many US brokers charge "
                        "no commission but spreads and regulatory fees exist). Slippage 0.10% assumed, not measured."},
}

# Human approval: when on, approved trades wait for you to click Approve.
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "false").strip().lower() in ("1", "true", "yes", "on")

# How often the team runs a full cycle, in seconds.
CYCLE_SECONDS = max(2, _get_int("CYCLE_SECONDS", 5))

PORT = _get_int("PORT", 8000)
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def grok_enabled() -> bool:
    return bool(XAI_API_KEY)
