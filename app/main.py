"""Web app for the Grok AI Trading Team.

Start it from the project folder with:

    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000
"""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backtest, config, cryptodata, decisions, grok, stockdata
from .engine import TradingFloor

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

DASHBOARD_DIR = config.ROOT / "dashboard"
floor = TradingFloor()


@asynccontextmanager
async def lifespan(_app):
    log.info("=" * 56)
    log.info("GROK AI TRADING TEAM | SIMULATION ONLY (offline demo data)")
    log.info("Dashboard: http://127.0.0.1:%s", config.PORT)
    log.info("Grok: %s", f"key set, NOT VERIFIED yet, model {config.XAI_MODEL}. Called only when you press a button"
             + (f" or every {config.GROK_AUTO_RESEARCH_SECONDS}s (auto research ON)" if config.GROK_AUTO_RESEARCH_SECONDS else "")
             if config.grok_enabled() else "OFFLINE DEMO (no API key needed)")
    log.info("=" * 56)
    threading.Thread(target=floor.loop, daemon=True).start()
    yield
    floor.stop()


app = FastAPI(title="Grok AI Trading Team", lifespan=lifespan)


@app.get("/api/state")
def get_state():
    return floor.state()


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "SIMULATION", "cycle": floor.cycle, "grok": config.grok_enabled(), "grok_state": grok.status()["state"]}


@app.post("/api/grok/test")
def grok_test():
    """One connection check (key + model). Research only; never trades."""
    ok, msg = floor.grok_test()
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/grok/research")
def grok_research():
    """One Grok research pass on the latest simulated data. Research only; never trades."""
    ok, msg = floor.grok_research()
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/toggle")
def toggle_trading():
    floor.running = not floor.running
    floor._say("System", "Trading resumed." if floor.running else "New trades paused. Stop-loss exits still active.")
    return {"running": floor.running}


@app.post("/api/approval-mode")
def toggle_approval():
    floor.require_approval = not floor.require_approval
    floor._say("System", "Human approval ON: every BUY waits for you." if floor.require_approval
               else "Human approval OFF: approved trades fill automatically.")
    return {"require_approval": floor.require_approval}


@app.post("/api/proposals/{pid}/approve")
def approve(pid: str):
    ok, msg = floor.decide(pid, approve=True)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/proposals/{pid}/reject")
def reject(pid: str):
    ok, msg = floor.decide(pid, approve=False)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


@app.get("/api/stocks")
def stocks():
    """Stored real stock data, one line per symbol. Read-only: never contacts Alpha Vantage."""
    return {"symbols": stockdata.overview(), "api_key_set": bool(config.ALPHAVANTAGE_API_KEY)}


@app.get("/api/stocks/{symbol}")
def stock(symbol: str):
    """Bars, indicators, Market Analyst analysis and data warnings for one stock. Read-only."""
    symbol = symbol.upper()
    if symbol not in stockdata.SYMBOLS:
        raise HTTPException(status_code=404, detail=f"Only {', '.join(stockdata.SYMBOLS)} are supported.")
    return stockdata.detail(symbol)


@app.get("/api/crypto")
def crypto():
    """Stored Kraken spot candles per pair and interval. Read-only: never contacts Kraken."""
    return {"pairs": cryptodata.overview(), "intervals": list(cryptodata.INTERVALS)}


@app.get("/api/crypto/{pair}")
def crypto_pair(pair: str, interval: int = 60):
    """Candles, indicators, Market Analyst report and data warnings for one pair (e.g. BTC-USD). Read-only."""
    pair = pair.upper().replace("-", "/")
    if pair not in cryptodata.PAIRS:
        raise HTTPException(status_code=404, detail=f"Only {', '.join(cryptodata.PAIRS)} are supported.")
    if interval not in cryptodata.INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {list(cryptodata.INTERVALS)} minutes.")
    return cryptodata.detail(pair, interval)


# ---------- Lesson 9: Trade Decision Center (SIMULATION ONLY, paper ledger in data/decisions.db) ----------
class Approval(BaseModel):
    content_hash: str  # the exact proposal the human saw; a changed proposal is never filled


@app.get("/api/decisions")
def decisions_state():
    """Proposals, paper positions, fills and performance. Read-only."""
    return decisions.state()


@app.post("/api/decisions/run")
def decisions_run():
    """One decision cycle on stored real data. Creates proposals only; nothing is executed without approval."""
    return {"ok": True, "proposals": decisions.run(halted=not floor.running)}


@app.post("/api/decisions/{pid}/approve")
def decisions_approve(pid: str, body: Approval):
    """Human approval: re-checks everything, then writes ONE simulated fill. Never contacts an exchange."""
    ok, out = decisions.approve(pid, body.content_hash, halted=not floor.running)
    if not ok:
        raise HTTPException(status_code=409, detail=out)
    return {"ok": True, "message": "Approved and filled (simulated paper fill).", "fill": out}


@app.post("/api/decisions/{pid}/reject")
def decisions_reject(pid: str):
    ok, msg = decisions.reject(pid)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


# ---------- Lesson 10: Risk Control Center ----------
class KillSwitchReset(BaseModel):
    reviewer: str
    note: str
    confirm: str        # must be decisions.RESET_PHRASE
    triggered_at: str   # the trigger the reviewer looked at; a newer trigger needs a new review


@app.post("/api/risk/check-stops")
def risk_check_stops():
    """Simulated stop-loss / take-profit on stored candles for every open paper position. Never contacts an exchange."""
    return {"ok": True, "events": decisions.check_stops()}


@app.post("/api/risk/kill-switch/reset")
def risk_kill_switch_reset(body: KillSwitchReset):
    """Clear the drawdown kill switch only after the server verifies an explicit, current, written review."""
    ok, msg = decisions.reset_kill_switch(body.reviewer, body.note, body.confirm, body.triggered_at)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


# ---------- Lesson 11: Backtesting Lab (SIMULATION / HISTORICAL BACKTEST, never the live ledger) ----------
@app.get("/api/backtest/options")
def backtest_options():
    return backtest.options()


@app.post("/api/backtest/run")
def backtest_run(body: dict):
    """Start ONE background backtest on stored data. Its approval policy exists only inside app/backtest.py."""
    try:
        return {"ok": True, "run_id": backtest.start(body)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except RuntimeError as err:
        raise HTTPException(status_code=409, detail=str(err))


@app.get("/api/backtest/runs")
def backtest_runs():
    return {"runs": backtest.list_runs()}


@app.get("/api/backtest/runs/{run_id}")
def backtest_result(run_id: str):
    r = backtest.get_run(run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Unknown backtest run.")
    return r


@app.post("/api/reset")
def reset():
    floor.reset()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(DASHBOARD_DIR / "index.html")


app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")
