"""Lesson 9 Trade Decision Engine (SIMULATION ONLY).

Market data comes from a fake `analyze` function or from a temp market DB filled through faked Kraken / CSV imports.
Nothing reaches the internet, Grok is never called, and nothing is approved except where a test calls approve().
"""

import contextlib
import datetime as dt
import inspect
import json
import re
import subprocess
import sys
import threading
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app import config, cryptodata, decisions, grok, stockdata, us_calendar
from app.market import market_type
from tests.test_cryptodata import candles, kraken, ohlc  # noqa: F401  (kraken is a fixture)
from tests.test_stockdata import raw_rows, write_csv

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # Monday 10:00 New York: US session open
TS = "2026-11-16T15:00Z"                               # close time of the latest candle in the fake views


def view(symbol, price=100.0, score=0.8, delayed=False, ts=TS, enough=True, prev=None, status="online"):
    """A Market Analyst report like decisions.market_view() returns."""
    return {"symbol": symbol, "type": market_type(symbol), "enough_data": enough, "status": "OK" if enough else "INSUFFICIENT DATA",
            "bars": 60 if enough else 3, "price": price, "prev_close": prev or price * 0.99, "trend": "up" if score > 0 else "down",
            "trend_score": score, "momentum_pct": score * 5, "volatility_pct": 1.0, "sma_fast": price, "sma_slow": price * 0.98,
            "ema_12": price, "ema_26": price * 0.99, "rsi_14": 60.0, "macd": {"macd": 1.0, "signal": 0.5, "histogram": 0.5},
            "volume_change_pct": 5.0, "delayed": delayed, "analysis_time": ts, "exchange": "TEST FIXTURE",
            "currency": "USD", "pair_status": status, "interval": "60m", "data_source": "TEST FIXTURE (not real data)",
            "warnings": [], "limitations": "test fixture"}


class Market:
    """Stands in for decisions.market_view. Unknown symbols have no data (NO TRADE)."""

    def __init__(self, **views):
        self.views = {k.replace("_", "-"): v for k, v in views.items()}

    def set(self, symbol, **kw):
        self.views[symbol] = view(symbol, **kw)

    def __call__(self, symbol, now):
        return self.views.get(symbol) or view(symbol, enough=False)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions, "DB_PATH", tmp_path / "decisions.db")


def run(market, now=NOW, **kw):
    return {p["symbol"]: p for p in decisions.run(now=now, analyze=market, symbols=list(market.views), **kw)}


def db_rows(sql, *args):
    with decisions._db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get(pid):
    return db_rows("SELECT * FROM proposals WHERE proposal_id=?", pid)[0]


def fills():
    return db_rows("SELECT * FROM fills")


def seed(symbol, qty, avg, cash=None, fresh=True, day_start=None, day_date=None):
    """Put a paper position / cash straight into the ledger (price analyzed at NOW). The daily-loss baseline and
    the peak are set to the new equity unless given, so a seeded balance is not mistaken for a loss."""
    with decisions._db() as conn:
        decisions._account(conn, NOW)
        if symbol:
            conn.execute("INSERT INTO positions (symbol, market_type, qty, avg_price, opened_at, mark_price, mark_time, mark_fresh) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (symbol, market_type(symbol).upper(), qty, avg, decisions._iso(NOW), avg, decisions._iso(NOW), int(fresh)))
        if cash is not None:
            conn.execute("UPDATE account SET cash=?", (cash,))
        equity = decisions._equity(conn, NOW)[0]
        conn.execute("UPDATE account SET day_start_equity=?, peak_equity=?", (day_start or equity, equity))
        if day_date:
            conn.execute("UPDATE account SET day_date=?", (day_date,))


def approve(p, market, now=NOW, **kw):
    return decisions.approve(p["proposal_id"], p["content_hash"], now=now, analyze=market, **kw)


# 1, 9, 15, 16 ----------------------------------------------------------------------------
def test_buy_waits_for_the_human_then_fills_once_with_fee_and_balance():
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0))
    p = run(market)["BTC-USD"]
    assert p["signal"] == "BUY" and p["risk_decision"] == "RISK_APPROVED"
    assert p["human_approval_status"] == "AWAITING_HUMAN_APPROVAL" and p["execution_status"] == "WAITING_FOR_HUMAN"
    assert p["proposed_quantity"] == pytest.approx(100_000 * config.MAX_POSITION_PCT / 50_000)       # 0.3 BTC
    assert p["reasoning"]["indicators"]["trend_score"] == 0.8 and p["research_source"].startswith("NONE")
    for key in ("proposal_id", "market_type", "exchange_or_provider", "symbol", "instrument_type", "signal", "proposed_quantity",
                "proposed_notional", "reference_price", "currency", "analysis_time_utc", "data_timestamp_utc", "reasoning",
                "supporting_research", "research_source", "confidence", "risk_decision", "risk_reason",
                "human_approval_status", "execution_status", "created_at", "expires_at"):
        assert key in get(p["proposal_id"])
    assert fills() == [] and db_rows("SELECT * FROM positions") == []                                # 9: nothing before approval
    assert db_rows("SELECT cash FROM account")[0]["cash"] == 100_000

    ok, fill = approve(p, market, now=NOW + dt.timedelta(seconds=30))
    assert ok, fill
    notional, fee = 0.3 * 50_000, 0.3 * 50_000 * config.FEE_PCT
    assert fill["notional"] == pytest.approx(notional) and fill["fee"] == pytest.approx(fee) == pytest.approx(15.0)
    assert fill["cash_before"] == 100_000 and fill["cash_after"] == pytest.approx(100_000 - notional - fee)
    assert fill["position_before"] == 0 and fill["position_after"] == pytest.approx(0.3) and fill["price"] == 50_000
    assert fill["proposal_id"] == p["proposal_id"] and fill["approved_at"] and fill["filled_at"]
    assert get(p["proposal_id"])["execution_status"] == "FILLED"
    [pos] = db_rows("SELECT * FROM positions")
    assert pos["qty"] == pytest.approx(0.3) and pos["avg_price"] == 50_000
    perf = decisions.state(now=NOW + dt.timedelta(seconds=31))["performance"]
    assert perf["equity"] == pytest.approx(100_000 - fee) and perf["fees_paid"] == 15.0 and perf["trade_count"] == 1
    assert perf["win_rate_pct"] == "N/A" and perf["approved_orders"] == 1


# 2 ---------------------------------------------------------------------------------------
def test_sell_reduces_a_held_position_and_books_realized_pnl():
    seed("BTC-USD", 0.3, 50_000.0, cash=85_000.0)
    market = Market(BTC_USD=view("BTC-USD", price=45_000.0, score=-0.8))
    p = run(market)["BTC-USD"]
    assert p["signal"] == "SELL" and p["risk_decision"] == "RISK_APPROVED" and p["proposed_quantity"] == pytest.approx(0.3)
    ok, fill = approve(p, market)
    assert ok, fill
    fee = 0.3 * 45_000 * config.FEE_PCT
    assert fill["realized_pnl"] == pytest.approx((45_000 - 50_000) * 0.3 - fee)
    assert fill["cash_after"] == pytest.approx(85_000 + 0.3 * 45_000 - fee) and fill["position_after"] == 0
    assert db_rows("SELECT * FROM positions") == []
    perf = decisions.state(now=NOW)["performance"]
    assert perf["closed_trades"] == 1 and perf["win_rate_pct"] == 0.0 and perf["realized_pnl"] == pytest.approx(-1513.5)


def test_sell_without_a_position_is_never_a_short():
    market = Market(ETH_USD=view("ETH-USD", score=-0.9))
    assert run(market)["ETH-USD"]["signal"] == "NO TRADE"                       # Strategy Agent: no shorts
    p = {"symbol": "ETH-USD", "signal": "SELL", "order_type": decisions.ORDER_TYPE, "reference_price": 100.0,
         "proposed_quantity": 1.0, "confidence": 0.9}
    with decisions._db() as conn:
        decision, reason, _, _ = decisions._risk(conn, p, market("ETH-USD", NOW), NOW, halted=False)
    assert decision == "RISK_REJECTED" and "no short selling" in reason


# 3, 4 ------------------------------------------------------------------------------------
def test_hold_and_no_trade_never_create_an_order():
    seed("BTC-USD", 0.3, 50_000.0, cash=85_000.0)
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0, score=0.1),            # held, weak: HOLD
                    ETH_USD=view("ETH-USD", score=0.2),                            # weak setup: NO TRADE
                    SOL_USD=view("SOL-USD", enough=False))                         # not enough data: NO TRADE
    out = run(market)
    assert [out[s]["signal"] for s in ("BTC-USD", "ETH-USD", "SOL-USD")] == ["HOLD", "NO TRADE", "NO TRADE"]
    for p in out.values():
        assert p["execution_status"] == "NO_ORDER" and p["risk_decision"] is None and p["proposed_quantity"] is None
        assert p["content_hash"] is None
        assert decisions.approve(p["proposal_id"], "anything", now=NOW, analyze=market)[0] is False
    assert any("Market data INSUFFICIENT DATA" in m for m in out["SOL-USD"]["reasoning"]["missing_or_uncertain"])
    assert out["ETH-USD"]["news_analysis"]["status"] == "NEWS_UNAVAILABLE"
    assert fills() == [] and decisions.state(now=NOW)["performance"]["no_trade"] == 2


# 5 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("setup, expected", [
    (lambda: [seed(s, 1, 100.0) for s in ("SPY", "QQQ", "AAPL", "NVDA", "TSLA")], "Max 5 open positions"),
    (lambda: seed(None, 0, 0, day_start=110_000.0, day_date="2026-11-16"), "Daily loss limit hit"),
    (lambda: seed("BTC-USD", 0.5996, 50_000.0, cash=70_020.0), "Not enough cash or exposure room"),   # crypto cap 30% full
])
def test_orders_over_a_risk_limit_are_blocked(setup, expected):
    setup()
    p = run(Market(ETH_USD=view("ETH-USD", price=2_000.0)))["ETH-USD"]
    assert p["signal"] == "BUY" and p["risk_decision"] == "RISK_REJECTED" and expected in p["risk_reason"]
    assert p["human_approval_status"] == "NOT_SENT" and p["execution_status"] == "BLOCKED"


def test_oversized_buy_is_resized_and_both_amounts_are_recorded():
    seed("ETH-USD", 10, 2_000.0, cash=80_000.0)              # 20% of equity already in crypto; crypto cap is 30%
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0))
    p = run(market)["BTC-USD"]
    assert p["risk_decision"] == "RISK_APPROVED"
    assert p["original_notional"] == pytest.approx(15_000.0) and p["proposed_notional"] == pytest.approx(10_000.0)
    assert "Resized from 0.3 (15,000.00) to 0.2 (10,000.00)" in p["size_adjustment"]
    ok, fill = approve(p, market)
    assert ok and fill["qty"] == pytest.approx(0.2)                          # the resized amount, not the original


def test_daily_loss_baseline_resets_on_a_new_day_and_fails_closed_without_fresh_prices():
    seed(None, 0, 0, day_start=110_000.0, day_date="2026-11-15")                 # yesterday's baseline, -9% vs today
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    assert p["risk_decision"] == "RISK_APPROVED"                                 # re-based for 2026-11-16 (old broker bug fixed here)
    assert db_rows("SELECT day_date, day_start_equity FROM account")[0] == {"day_date": "2026-11-16", "day_start_equity": 100_000}
    decisions.reject(p["proposal_id"], now=NOW)
    seed("SPY", 10, 500.0, fresh=False)                                          # held, but its price is not fresh
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "daily loss state reliable" in p["risk_reason"]


# 6 ---------------------------------------------------------------------------------------
def test_buy_without_enough_cash_is_blocked():
    seed(None, 0, 0, cash=40.0)
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "Not enough cash" in p["risk_reason"] and fills() == []


# 7 ---------------------------------------------------------------------------------------
def test_stale_or_abnormal_data_is_blocked():
    out = run(Market(BTC_USD=view("BTC-USD", delayed=True), ETH_USD=view("ETH-USD", price=200.0, prev=100.0)))
    assert out["BTC-USD"]["risk_decision"] == "RISK_REJECTED" and "data freshness" in out["BTC-USD"]["risk_reason"]
    assert out["ETH-USD"]["risk_decision"] == "RISK_REJECTED" and "abnormal price" in out["ETH-USD"]["risk_reason"]


# 8 ---------------------------------------------------------------------------------------
def test_risk_rejected_proposal_can_never_be_executed():
    market = Market(BTC_USD=view("BTC-USD", delayed=True))
    p = run(market)["BTC-USD"]
    market.set("BTC-USD")                                                        # data is fine again
    assert approve(p, market)[0] is False and fills() == []
    assert get(p["proposal_id"])["human_approval_status"] == "NOT_SENT"


# 10 --------------------------------------------------------------------------------------
def test_human_rejection_blocks_the_order():
    market = Market(BTC_USD=view("BTC-USD"))
    p = run(market)["BTC-USD"]
    assert decisions.reject(p["proposal_id"], now=NOW) == (True, "Rejected. No order.")
    ok, msg = approve(p, market)
    assert not ok and "HUMAN_REJECTED" in msg and fills() == []
    assert get(p["proposal_id"])["execution_status"] == "CANCELLED"


# 11 --------------------------------------------------------------------------------------
def test_expired_proposal_is_blocked():
    market = Market(BTC_USD=view("BTC-USD"))
    p = run(market)["BTC-USD"]
    ok, msg = approve(p, market, now=NOW + dt.timedelta(seconds=decisions.EXPIRES_SECONDS + 1))
    assert not ok and "EXPIRED" in msg and fills() == []
    assert get(p["proposal_id"])["human_approval_status"] == "EXPIRED"


# 12 --------------------------------------------------------------------------------------
def test_approval_re_runs_the_risk_checks():
    market = Market(BTC_USD=view("BTC-USD"), ETH_USD=view("ETH-USD"), SOL_USD=view("SOL-USD"), AAPL=view("AAPL", price=200.0))
    out = run(market)
    assert all(p["risk_decision"] == "RISK_APPROVED" for p in out.values())

    ok, msg = approve(out["BTC-USD"], market, halted=True)                       # emergency stop pressed after the proposal
    assert not ok and "simulation running" in msg
    market.set("ETH-USD", ts="2026-11-16T16:00Z")                               # a newer candle arrived
    ok, msg = approve(out["ETH-USD"], market)
    assert not ok and "market data changed" in msg
    seed(None, 0, 0, cash=5_000.0)                                               # cash dropped below the approved size
    ok, msg = approve(out["SOL-USD"], market)
    assert not ok and "size within current limits" in msg
    ok, msg = decisions.approve(out["AAPL"]["proposal_id"], "0" * 16, now=NOW, analyze=market)  # not what the human saw
    assert not ok and "content hash mismatch" in msg
    assert fills() == []
    assert {get(p["proposal_id"])["execution_status"] for p in out.values()} == {"BLOCKED"}
    assert decisions.state(now=NOW)["performance"]["blocked_at_approval"] == 4


# 13 --------------------------------------------------------------------------------------
def test_double_click_and_parallel_approvals_fill_exactly_once():
    market = Market(BTC_USD=view("BTC-USD"))
    p = run(market)["BTC-USD"]
    assert run(market)["BTC-USD"]["risk_reason"].startswith("duplicate proposal")   # same symbol already waiting
    results = []
    threads = [threading.Thread(target=lambda: results.append(approve(p, market))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(ok for ok, _ in results) == 1 and len(fills()) == 1
    assert approve(p, market) == (False, "Not executed: proposal is HUMAN_APPROVED / FILLED (already handled).")


# 14 --------------------------------------------------------------------------------------
def test_approval_and_trade_state_survive_a_server_restart():
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0), ETH_USD=view("ETH-USD"), SOL_USD=view("SOL-USD"))
    out = run(market)
    assert approve(out["BTC-USD"], market)[0]
    decisions.reject(out["ETH-USD"]["proposal_id"], now=NOW)
    later = NOW + dt.timedelta(minutes=10)                                       # a fresh process, 10 minutes later
    code = ("import datetime as dt, json, pathlib, sys; sys.path.insert(0, '.'); from app import decisions; "
            f"decisions.DB_PATH = pathlib.Path(r'{decisions.DB_PATH}'); s = decisions.state(dt.datetime.fromisoformat('{later.isoformat()}')); "
            "print(json.dumps({'status': {p['symbol']: p['human_approval_status'] for p in s['history']}, "
            "'fills': len(s['fills']), 'cash': s['account']['cash']}))")
    after = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert after["status"] == {"BTC-USD": "HUMAN_APPROVED", "ETH-USD": "HUMAN_REJECTED", "SOL-USD": "EXPIRED"}  # never approved
    assert after["fills"] == 1 and after["cash"] == pytest.approx(100_000 - 15_000 - 15)
    assert approve(out["BTC-USD"], market)[0] is False and len(fills()) == 1   # still exactly one fill


# 17 --------------------------------------------------------------------------------------
@pytest.fixture
def market_db(tmp_path, monkeypatch, kraken):  # noqa: F811
    """Real Lesson 6 / 7 code paths on a temp market DB: SPY from CSV, BTC/USD from (faked) Kraken."""
    monkeypatch.setattr(stockdata, "DB_PATH", tmp_path / "market.db")
    with contextlib.closing(cryptodata.connect()) as conn:
        stockdata.import_csv(write_csv(tmp_path / "spy.csv", raw_rows(40)), conn, symbol="SPY", now_utc=NOW)
        kraken.replies["OHLC"] = ohlc(candles(40, end=int(dt.datetime(2026, 11, 16, 14, tzinfo=UTC).timestamp())))
        cryptodata.fetch_kraken("BTC/USD", 60, conn, NOW)
    return kraken


def test_stock_and_crypto_data_are_kept_apart(market_db):
    out = {p["symbol"]: p for p in decisions.run(now=NOW, symbols=["SPY", "BTC-USD"])}
    spy, btc = out["SPY"], out["BTC-USD"]
    assert (spy["market_type"], spy["instrument_type"], spy["currency"]) == ("STOCK", "ETF", "USD")
    assert (btc["market_type"], btc["instrument_type"], btc["exchange_or_provider"], btc["currency"]) == ("CRYPTO_SPOT", "SPOT", "KRAKEN", "USD")
    assert spy["market_analysis"]["interval"] == "1d" and btc["market_analysis"]["interval"] == "60m"
    assert spy["data_timestamp_utc"] == "2026-11-13" and btc["data_timestamp_utc"] == "2026-11-16T15:00Z"
    assert "New York" in spy["market_analysis"]["trading_hours"] and "24/7" in btc["market_analysis"]["trading_hours"]
    assert spy["signal"] == btc["signal"] == "BUY" and spy["risk_decision"] == btc["risk_decision"] == "RISK_APPROVED"
    assert spy["proposed_quantity"] == int(spy["proposed_quantity"])           # whole shares
    assert btc["proposed_quantity"] != int(btc["proposed_quantity"])           # fractional crypto
    assert spy["reference_price"] == 139.0 and btc["reference_price"] == 139.0  # each from its own table
    assert all("HISTORICAL" in p["data_mode"] for p in out.values())
    sunday = dt.datetime(2026, 11, 15, 15, 0, tzinfo=UTC)                       # same data, US market closed
    assert approve(spy, None, now=sunday)[0] is False and "market open" in get(spy["proposal_id"])["risk_reason"]
    ok, fill = approve(btc, None, now=NOW + dt.timedelta(seconds=10))           # crypto trades 24/7
    assert ok and fill["price"] == 139.0


def test_us_session_hours():
    at = lambda *a: dt.datetime(*a, tzinfo=UTC)  # noqa: E731
    assert not us_calendar.session_open(at(2026, 11, 16, 14, 29)) and us_calendar.session_open(at(2026, 11, 16, 14, 30))
    assert us_calendar.session_open(at(2026, 11, 16, 20, 59)) and not us_calendar.session_open(at(2026, 11, 16, 21, 0))
    assert us_calendar.session_open(at(2026, 7, 1, 13, 30))                     # summer time: 09:30 EDT
    assert not us_calendar.session_open(at(2026, 11, 15, 15, 0))                # Sunday
    assert not us_calendar.session_open(at(2026, 11, 26, 15, 0))                # Thanksgiving


# 18 --------------------------------------------------------------------------------------
def test_prediction_market_proposals_are_blocked():
    p = run(Market(PM_FED_CUT_DEC=view("PM-FED-CUT-DEC", price=0.6, prev=0.59)))["PM-FED-CUT-DEC"]
    assert p["signal"] == "BUY" and p["risk_decision"] == "RISK_REJECTED" and "supported market" in p["risk_reason"]
    real = decisions.run(now=NOW, symbols=["PM-BTC-100K-EOY"])[0]              # default Market Analyst: no real data
    assert real["signal"] == "NO TRADE" and real["market_type"] == "PREDICTION"
    assert real["reasoning"]["strategy_note"].startswith("Unsupported market")


# 20 --------------------------------------------------------------------------------------
def test_no_network_no_grok_and_no_order_api(market_db, monkeypatch):
    monkeypatch.setattr(config, "XAI_API_KEY", "xai-FAKEFAKEFAKEFAKE")                # even with a key set
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or pytest.fail("network call"))
    before = grok.status()["requests"]
    out = {p["symbol"]: p for p in decisions.run(now=NOW)}
    assert approve(out["BTC-USD"], None, now=NOW)[0]
    assert calls == [] and grok.status()["requests"] == before
    source = inspect.getsource(decisions)
    assert not re.search(r"https?://|urlopen|requests\.|import grok|ask_grok", source)
    assert not re.search(r"^\s*(?:import|from)\s+(alpaca|ccxt|ib_insync|binance|robin_stocks|schwab|tda)\b", source, re.M)


# API + end-to-end -------------------------------------------------------------------------
def test_decision_api(monkeypatch):
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0))
    monkeypatch.setattr(decisions, "market_view", market)
    from app.main import app
    with TestClient(app) as client:
        props = {p["symbol"]: p for p in client.post("/api/decisions/run").json()["proposals"]}
        assert len(props) == len(decisions.universe()) and props["BTC-USD"]["signal"] == "BUY"
        pid, h = props["BTC-USD"]["proposal_id"], props["BTC-USD"]["content_hash"]
        assert client.post(f"/api/decisions/{pid}/approve", json={}).status_code == 422          # hash required
        res = client.post(f"/api/decisions/{pid}/approve", json={"content_hash": h})
        assert res.status_code == 200 and res.json()["fill"]["side"] == "BUY"
        again = client.post(f"/api/decisions/{pid}/approve", json={"content_hash": h})
        assert again.status_code == 409 and "already handled" in again.json()["detail"]
        assert client.post("/api/decisions/nope/reject").status_code == 409
        s = client.get("/api/decisions").json()
        assert s["mode"] == "SIMULATION ONLY" and len(s["fills"]) == 1 and len(s["latest_run"]) == len(decisions.universe())
        assert client.get("/api/state").json()["mode"] == "SIMULATION"                       # old simulator still served


def test_end_to_end_normal_risk_blocked_and_human_rejected(capsys):
    seed("BTC-USD", 0.5996, 50_000.0, cash=70_020.0)       # 29,980 in crypto already: the 30% cap is almost full
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0, score=0.1), SPY=view("SPY", price=500.0),
                    ETH_USD=view("ETH-USD", price=2_000.0), QQQ=view("QQQ", price=400.0, score=0.5))
    out = run(market)
    spy, eth, qqq = out["SPY"], out["ETH-USD"], out["QQQ"]
    assert out["BTC-USD"]["signal"] == "HOLD"
    assert spy["risk_decision"] == "RISK_APPROVED" and eth["risk_decision"] == "RISK_REJECTED" and qqq["risk_decision"] == "NEEDS_REVIEW"
    ok, fill = approve(spy, market)
    assert ok and fill["qty"] == 30 and fill["cash_after"] == pytest.approx(70_020 - 15_000 - 15)
    assert approve(eth, market)[0] is False
    assert decisions.reject(qqq["proposal_id"], now=NOW)[0]
    perf = decisions.state(now=NOW)["performance"]
    assert (perf["approved_orders"], perf["risk_rejected"], perf["human_rejected"], perf["hold"]) == (1, 1, 1, 1)
    assert len(fills()) == 1
    with capsys.disabled():  # visible with `pytest -s`: the actual inputs and outputs of each step
        for p in (spy, eth, qqq):
            row = get(p["proposal_id"])
            print(f"\n{p['symbol']}: market trend {p['market_analysis']['trend_score']} price {p['reference_price']} | news "
                  f"{p['news_analysis']['status']} | strategy {p['signal']} qty {p['proposed_quantity']} | risk {p['risk_decision']}: "
                  f"{p['risk_reason'][:90]} | human {row['human_approval_status']} | execution {row['execution_status']}")
        print("performance:", {k: perf[k] for k in ("equity", "cash", "fees_paid", "approved_orders", "risk_rejected", "human_rejected")})
