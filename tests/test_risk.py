"""Lesson 10 risk management (SIMULATION ONLY): position / exposure / per-trade limits, daily loss, simulated
stop-loss, losing-streak cooldown, drawdown kill switch, approval integration and persistence.

Market data comes from fake views / fake candle loaders or a temp market DB filled through faked Kraken / CSV imports.
Every test uses its own temp decisions.db. No internet, no Grok, nothing approved unless a test calls approve().
"""

import datetime as dt
import json
import sqlite3
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from app import config, cryptodata, decisions
from app.engine import TradingFloor
from app.market import market_type
from tests.test_cryptodata import kraken  # noqa: F401  (fixture used by market_db)
from tests.test_decisions import NOW, TS, Market, market_db, view  # noqa: F401

H = 3600
T0 = decisions._ts(TS)  # 15:00 UTC: the first candle after an entry made on the 15:00 close


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions, "DB_PATH", tmp_path / "decisions.db")


def iso(t=NOW):
    return decisions._iso(t)


def rows(sql, *args):
    with decisions._db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def events(kind):
    return [{**e, "detail": json.loads(e["detail"])} for e in rows("SELECT * FROM risk_events WHERE kind=? ORDER BY event_id", kind)]


def candle(i, o, h, low, c, v=10.0):
    return {"open_time": T0 + i * H, "close_time": T0 + (i + 1) * H, "open": o, "high": h, "low": low, "close": c, "volume": v}


def loader(bars, delayed=False):
    return lambda symbol, since, now: ([b for b in bars if b["open_time"] >= since], delayed, H)


def seed(symbol=None, qty=0.0, avg=0.0, mark=None, cash=None, fresh=True, stop=None, peak=None, day_start=None, day_date=None):
    """Write a position / balance straight into the test ledger. Baseline and peak default to the new equity."""
    with decisions._db() as conn:
        decisions._account(conn, NOW)
        if symbol:
            decisions._insert(conn, "positions", {
                "symbol": symbol, "market_type": decisions.MARKET_CLASS[market_type(symbol)], "qty": qty, "avg_price": avg,
                "opened_at": iso(), "mark_price": mark or avg, "mark_time": iso(), "mark_fresh": int(fresh), "stop_price": stop,
                "take_profit_price": None, "entry_fees": 0.0, "checked_until": T0 if stop else None, "entry_proposal_id": "seeded"})
        if cash is not None:
            conn.execute("UPDATE account SET cash=?", (cash,))
        equity = decisions._equity(conn, NOW)[0]
        conn.execute("UPDATE account SET day_start_equity=?, peak_equity=?", (day_start or equity, peak or equity))
        if day_date:
            conn.execute("UPDATE account SET day_date=?", (day_date,))


def run(market, now=NOW, **kw):
    return {p["symbol"]: p for p in decisions.run(now=now, analyze=market, symbols=list(market.views), candles=loader([]), **kw)}


def approve(p, market, now=NOW, **kw):
    return decisions.approve(p["proposal_id"], p["content_hash"], now=now, analyze=market, **kw)


def bought(symbol="BTC-USD", price=50_000.0, now=NOW):
    market = Market(**{symbol.replace("-", "_"): view(symbol, price=price)})
    p = run(market, now)[symbol]
    ok, fill = approve(p, market, now)
    assert ok, fill
    return p, market


def risk(p, market, **kw):
    """Run the Risk Manager on a hand-made proposal (e.g. a BUY on a held symbol the Strategy Agent would not make)."""
    base = {"proposal_id": "manual", "order_type": decisions.ORDER_TYPE, "confidence": 0.9, "currency": "USD"}
    with decisions._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        out = decisions._risk(conn, {**base, **p}, market(p["symbol"], NOW), NOW, halted=False, **kw)
        conn.execute("ROLLBACK")
    return out


def binding(checks):
    return next(c["check"] for c in checks if c["detail"].endswith("BINDING"))


# A ----------------------------------------------------------------------------------------
def test_A_normal_position_is_approved_with_stop_and_take_profit():
    p, market = bought()
    assert p["risk_decision"] == "RISK_APPROVED" and p["stop_price"] == pytest.approx(47_500) and p["take_profit_price"] == pytest.approx(55_000)
    limits = {c["check"]: c["detail"] for c in p["risk_checks"] if c["check"].startswith("limit: ")}
    assert set(limits) == {"limit: position size", "limit: CRYPTO_SPOT exposure", "limit: total exposure", "limit: per-trade risk",
                           "limit: cash", "limit: Risk Manager rules"}
    [pos] = rows("SELECT * FROM positions")
    assert pos["stop_price"] == pytest.approx(47_500) and pos["checked_until"] == T0 and pos["entry_fees"] == pytest.approx(15.0)
    assert pos["entry_proposal_id"] == p["proposal_id"] and events("TRADE_FILLED")[0]["proposal_id"] == p["proposal_id"]


# B ----------------------------------------------------------------------------------------
def test_B_position_limit_counts_what_is_already_held():
    seed("BTC-USD", 0.2, 50_000.0, cash=90_000.0)                       # 10,000 held (10% of equity 100,000)
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0))
    order = {"symbol": "BTC-USD", "signal": "BUY", "reference_price": 50_000.0, "proposed_quantity": 0.3, "stop_price": 47_500.0}
    decision, _, checks, allowed = risk(order, market)
    assert decision == "RISK_APPROVED" and allowed == pytest.approx(0.1) and binding(checks) == "limit: position size"
    with decisions._db() as conn:
        conn.execute("UPDATE positions SET qty=0.3 WHERE symbol='BTC-USD'")  # now 15,000 held = the full 15%
    seed(cash=85_000.0)                                                      # equity stays 100,000
    decision, reason, _, _ = risk(order, market)
    assert decision == "RISK_REJECTED" and "minimum order" in reason and "position size" in reason


def test_B_position_limit_rejects_bad_quantity():
    market = Market(BTC_USD=view("BTC-USD"))
    for qty in (0, -1, float("nan"), float("inf")):
        decision, reason, _, _ = risk({"symbol": "BTC-USD", "signal": "BUY", "reference_price": 100.0, "proposed_quantity": qty,
                                       "stop_price": 95.0}, market)
        assert decision == "RISK_REJECTED" and "valid quantity" in reason
    stock = Market(AAPL=view("AAPL", price=200.0))
    assert "valid quantity" in risk({"symbol": "AAPL", "signal": "BUY", "reference_price": 200.0, "proposed_quantity": 2.5,
                                     "stop_price": 190.0}, stock)[1]          # no fractional shares


# C ----------------------------------------------------------------------------------------
def test_C_total_exposure_resizes_then_rejects():
    for sym in ("SPY", "QQQ", "AAPL", "NVDA"):
        seed(sym, 100, 175.0)                                                # 4 x 17,500 = 70,000
    seed(cash=30_000.0)                                                      # equity 100,000, exposure 70%
    p = run(Market(ETH_USD=view("ETH-USD", price=2_000.0)))["ETH-USD"]
    assert p["risk_decision"] == "RISK_APPROVED" and p["proposed_notional"] == pytest.approx(5_000.0)  # 75% cap leaves 5,000
    assert "limited by total exposure" in p["size_adjustment"] and p["original_notional"] == pytest.approx(15_000.0)
    decisions.reject(p["proposal_id"], now=NOW)
    seed(cash=25_000.0)
    with decisions._db() as conn:
        conn.execute("UPDATE positions SET qty=qty*75000.0/70000.0")       # 75,000 held: no room left
    p = run(Market(ETH_USD=view("ETH-USD", price=2_000.0)))["ETH-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "total exposure" in p["risk_reason"]


# D ----------------------------------------------------------------------------------------
def test_D_market_exposure_uses_market_value_and_market_classes():
    seed("ETH-USD", 12.5, 1_800.0, mark=2_000.0, cash=75_000.0)            # crypto 25,000 at market (22,500 at cost)
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    assert p["proposed_notional"] == pytest.approx(5_000.0) and "CRYPTO_SPOT exposure" in p["size_adjustment"]
    s = decisions.state(now=NOW)["risk"]["exposure"]
    assert s["CRYPTO_SPOT_pct"] == 25.0 and s["STOCK_pct"] == 0 and s["PREDICTION"] == "UNSUPPORTED" and s["total_pct"] == 25.0
    pm = run(Market(PM_FED_CUT_DEC=view("PM-FED-CUT-DEC", price=0.6, prev=0.59)))["PM-FED-CUT-DEC"]
    assert pm["market_type"] == "PREDICTION" and pm["risk_decision"] == "RISK_REJECTED" and "UNSUPPORTED" in pm["risk_reason"]
    eur = Market(SOL_USD={**view("SOL-USD"), "currency": "EUR"})
    p = run(eur)["SOL-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "no verified FX rate" in p["risk_reason"]


# E ----------------------------------------------------------------------------------------
def test_E_per_trade_risk_caps_the_planned_loss_incl_fees_and_slippage(monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)                # 500 max loss on 100,000
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    unit_risk = 50_000 * 1.002 - 47_500 * 0.998                             # 2,695 per BTC incl. fee + slippage both ways
    assert p["risk_decision"] == "RISK_APPROVED" and "per-trade risk" in p["size_adjustment"]
    assert p["proposed_quantity"] == pytest.approx(500 / unit_risk, abs=1e-8) and p["proposed_quantity"] * unit_risk <= 500
    decisions.reject(p["proposal_id"], now=NOW)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.00002)              # 2 max loss -> about 37 USD of BTC: below minimum
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "minimum order" in p["risk_reason"] and "per-trade risk" in p["risk_reason"]


def test_E_strictest_limit_wins():
    seed("ETH-USD", 10, 2_000.0, cash=80_000.0)                             # crypto room 10,000; size 15,000; risk ~18,500
    p = run(Market(BTC_USD=view("BTC-USD", price=50_000.0)))["BTC-USD"]
    caps = {c["check"]: float(c["detail"].split(" = ")[1].split(" |")[0].replace(",", "")) for c in p["risk_checks"]
            if c["check"].startswith("limit: ")}
    assert p["proposed_notional"] == pytest.approx(min(caps.values())) == pytest.approx(10_000)


# F ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("stop", [None, 50_000.0, 51_000.0, 0.0, -5.0])
def test_F_missing_or_invalid_stop_blocks_new_entries(stop):
    order = {"symbol": "BTC-USD", "signal": "BUY", "reference_price": 50_000.0, "proposed_quantity": 0.1, "stop_price": stop}
    decision, reason, _, _ = risk(order, Market(BTC_USD=view("BTC-USD", price=50_000.0)))
    assert decision == "RISK_REJECTED" and "stop-loss price" in reason


# G ----------------------------------------------------------------------------------------
def test_G_daily_loss_limit_blocks_buys_but_allows_risk_reducing_sells():
    seed("BTC-USD", 0.3, 50_000.0, mark=50_000.0, cash=85_000.0, day_start=110_000.0)   # -9.1% today
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0, score=-0.8), ETH_USD=view("ETH-USD"))
    out = run(market)
    assert out["ETH-USD"]["risk_decision"] == "RISK_REJECTED" and "Daily loss limit hit" in out["ETH-USD"]["risk_reason"]
    sell = out["BTC-USD"]
    assert sell["signal"] == "SELL" and sell["risk_decision"] == "RISK_APPROVED"
    assert approve(sell, market)[0] and rows("SELECT * FROM positions") == []


# H ----------------------------------------------------------------------------------------
def test_H_new_utc_day_sets_a_new_baseline_but_never_clears_the_kill_switch():
    seed(day_start=110_000.0, day_date="2026-11-15")                        # yesterday's baseline
    p = run(Market(BTC_USD=view("BTC-USD")))["BTC-USD"]
    assert p["risk_decision"] == "RISK_APPROVED"
    [e] = events("DAILY_BASELINE")
    assert e["detail"]["date"] == "2026-11-16" and e["detail"]["previous_baseline"] == 110_000 and e["detail"]["baseline"] == 100_000
    decisions.reject(p["proposal_id"], now=NOW)
    seed(peak=120_000.0)
    run(Market(BTC_USD=view("BTC-USD", score=0.1)))                         # drawdown 16.7% -> kill switch
    tomorrow = NOW + dt.timedelta(days=1)
    p = run(Market(BTC_USD=view("BTC-USD")), now=tomorrow)["BTC-USD"]
    assert decisions.state(now=tomorrow)["account"]["day_date"] == "2026-11-17"
    assert p["risk_decision"] == "RISK_REJECTED" and "drawdown kill switch: ACTIVE" in p["risk_reason"]


def test_H_corrupted_daily_state_fails_closed():
    seed()
    with decisions._db() as conn:
        conn.execute("UPDATE account SET day_date='not-a-date'")
    p = run(Market(BTC_USD=view("BTC-USD")))["BTC-USD"]
    assert p["risk_decision"] == "RISK_REJECTED" and "daily-loss state is unreadable" in p["risk_reason"]


# J K L ------------------------------------------------------------------------------------
def test_J_stop_loss_fills_below_the_stop_with_slippage_and_both_fees():
    p, _ = bought()
    bars = [candle(0, 50_000, 50_500, 49_000, 49_500), candle(1, 49_500, 49_600, 47_000, 47_200)]
    out = decisions.check_stops(NOW + dt.timedelta(hours=3), loader(bars))
    [e] = out
    fill_price = 47_500 * (1 - config.SLIPPAGE_PCT)
    assert e["kind"] == "STOP_LOSS" and e["fill_price"] == pytest.approx(fill_price)
    [_, f] = rows("SELECT * FROM fills ORDER BY fill_id")
    assert f["side"] == "SELL" and f["qty"] == pytest.approx(0.3)
    assert f["realized_pnl"] == pytest.approx((fill_price - 50_000) * 0.3 - 0.3 * fill_price * config.FEE_PCT - 15.0)
    exit_row = rows("SELECT * FROM proposals WHERE proposal_id=?", f["proposal_id"])[0]
    assert exit_row["human_approval_status"] == decisions.BRACKET and exit_row["execution_status"] == "FILLED"
    assert rows("SELECT * FROM positions") == []
    assert decisions.check_stops(NOW + dt.timedelta(hours=4), loader(bars)) == [] and len(rows("SELECT * FROM fills")) == 2


def test_K_gap_down_fills_at_the_open_not_at_the_stop():
    bought()
    [e] = decisions.check_stops(NOW + dt.timedelta(hours=2), loader([candle(0, 46_000, 46_500, 45_500, 46_200)]))
    assert e["kind"] == "STOP_LOSS_GAP" and e["fill_price"] == pytest.approx(46_000 * (1 - config.SLIPPAGE_PCT))
    assert "not at the stop" in e["note"]


@pytest.mark.parametrize("bar, kind, price", [
    (candle(0, 50_500, 56_000, 47_000, 51_000), "STOP_LOSS_AMBIGUOUS", 47_500 * (1 - config.SLIPPAGE_PCT)),  # both touched
    (candle(0, 50_500, 55_500, 49_800, 55_200), "TAKE_PROFIT", 55_000),
    (candle(0, 56_000, 57_000, 55_800, 56_500), "TAKE_PROFIT_GAP", 55_000),                                   # no favorable gap assumed
])
def test_L_same_candle_and_take_profit_cases(bar, kind, price):
    bought()
    [e] = decisions.check_stops(NOW + dt.timedelta(hours=2), loader([bar]))
    assert e["kind"] == kind and e["fill_price"] == pytest.approx(price)
    if kind == "STOP_LOSS_AMBIGUOUS":
        assert "worse case (stop first)" in e["note"]


# M N --------------------------------------------------------------------------------------
def three_losses():
    """Three real round trips (BUY approved -> stop-loss), each a loss after fees."""
    for k, sym in enumerate(("BTC-USD", "ETH-USD", "SOL-USD")):
        t = NOW + dt.timedelta(hours=2 * k)
        bought(sym, price=100.0, now=t)
        decisions.check_stops(t + dt.timedelta(hours=1), loader([candle(0, 100, 100.5, 90, 91)]))
    return NOW + dt.timedelta(hours=5)


def test_M_unrealized_losses_are_not_closed_losing_trades():
    seed("AAPL", 100, 200.0, mark=120.0, cash=80_000.0)                     # -8,000 UNREALIZED: not a closed trade
    r = decisions.state(now=NOW)["risk"]
    assert r["losing_streak"] == 0 and not r["cooldown"]["active"]


def test_M_losing_streak_starts_a_cooldown_from_realized_pnl_only():
    t = three_losses()
    pnls = [f["realized_pnl"] for f in rows("SELECT * FROM fills WHERE side='SELL' ORDER BY fill_id")]
    assert len(pnls) == 3 and all(x < 0 for x in pnls)
    assert pnls[0] == pytest.approx((94.905 - 100) * 150 - 150 * 94.905 * 0.001 - 15.0)   # both fees inside
    [e] = events("COOLDOWN_STARTED")
    st = decisions.state(now=t)["risk"]
    assert st["losing_streak"] == 3 and st["cooldown"]["active"] and st["cooldown"]["until"] == e["detail"]["until"]
    assert dt.datetime.fromisoformat(st["cooldown"]["until"]) == dt.datetime.fromisoformat(e["time"]) + dt.timedelta(hours=config.COOLDOWN_HOURS)


def test_N_cooldown_blocks_entries_allows_exits_and_ends_on_time():
    t = three_losses()
    seed("BTC-USD", 0.1, 100.0, mark=100.0)
    with decisions._db() as conn:
        conn.execute("UPDATE positions SET mark_time=?", (iso(t),))
    market = Market(ETH_USD=view("ETH-USD"), BTC_USD=view("BTC-USD", price=100.0, score=-0.8))
    out = run(market, now=t)
    assert out["ETH-USD"]["risk_decision"] == "RISK_REJECTED" and "losing-streak cooldown: ACTIVE" in out["ETH-USD"]["risk_reason"]
    assert out["BTC-USD"]["signal"] == "SELL" and approve(out["BTC-USD"], market, now=t)[0]          # exit still allowed
    until = dt.datetime.fromisoformat(decisions.state(now=t)["risk"]["cooldown"]["until"])
    p = run(Market(ETH_USD=view("ETH-USD")), now=until + dt.timedelta(minutes=1))["ETH-USD"]
    assert p["risk_decision"] == "RISK_APPROVED" and len(events("COOLDOWN_ENDED")) == 1


# O P Q ------------------------------------------------------------------------------------
def test_O_P_Q_kill_switch_latches_blocks_buys_and_allows_exits():
    seed("BTC-USD", 0.3, 50_000.0, cash=85_000.0, peak=112_000.0)           # equity 100,000 vs peak 112,000 = 10.7%
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0, score=0.1), ETH_USD=view("ETH-USD"))
    out = run(market)
    ks = decisions.state(now=NOW)["risk"]["kill_switch"]
    assert ks["active"] and ks["triggered_at"] == iso() and ks["equity_at_trigger"] == 100_000 and ks["peak_at_trigger"] == 112_000
    [e] = events("KILL_SWITCH_TRIGGERED")
    assert "10.71%" in e["detail"]["reason"] and e["detail"]["cash"] == 85_000 and e["detail"]["positions"] == {"BTC-USD": 0.3}
    assert out["ETH-USD"]["risk_decision"] == "RISK_REJECTED" and "drawdown kill switch: ACTIVE" in out["ETH-USD"]["risk_reason"]

    recovered = Market(BTC_USD=view("BTC-USD", price=90_000.0, prev=89_500.0, score=0.1), ETH_USD=view("ETH-USD"))
    out = run(recovered)                                                     # equity 112,000 again: still latched
    assert out["ETH-USD"]["risk_decision"] == "RISK_REJECTED" and decisions.state(now=NOW)["risk"]["kill_switch"]["active"]

    too_much = {"symbol": "BTC-USD", "signal": "SELL", "reference_price": 90_000.0, "proposed_quantity": 0.5}
    assert "no short selling" in risk(too_much, recovered)[1]                # never more than held
    sell_market = Market(BTC_USD=view("BTC-USD", price=90_000.0, prev=89_500.0, score=-0.8))
    sell = run(sell_market)["BTC-USD"]
    assert sell["signal"] == "SELL" and sell["risk_decision"] == "RISK_APPROVED"
    ok, fill = approve(sell, sell_market)
    assert ok and fill["qty"] == pytest.approx(0.3) and fill["position_after"] == 0


# I R --------------------------------------------------------------------------------------
def test_I_R_risk_state_survives_a_restart(tmp_path):
    t = three_losses()
    seed(peak=120_000.0)
    run(Market(BTC_USD=view("BTC-USD", score=0.1)), now=t)                  # kill switch
    code = ("import datetime as dt, json, pathlib, sys; sys.path.insert(0, '.'); from app import decisions; "
            f"decisions.DB_PATH = pathlib.Path(r'{decisions.DB_PATH}'); "
            f"r = decisions.state(dt.datetime.fromisoformat('{t.isoformat()}'))['risk']; "
            "print(json.dumps([r['kill_switch']['active'], r['cooldown']['active'], r['losing_streak'], r['daily']['date'], "
            "len(r['settings_history'])]))")
    after = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert after == [True, True, 3, "2026-11-16", 1]


def test_I_settings_history_and_lesson9_database_upgrade(tmp_path, monkeypatch):
    old = tmp_path / "lesson9.db"
    with sqlite3.connect(old) as conn:                                       # a Lesson 9 file: old tables, no new columns
        conn.executescript(decisions.SCHEMA.split("CREATE TABLE IF NOT EXISTS risk_state")[0])
        conn.execute("INSERT INTO positions VALUES ('BTC-USD', 'CRYPTO', 0.1, 50000, ?, 50000, ?, 1)", (iso(), iso()))
    monkeypatch.setattr(decisions, "DB_PATH", old)
    decisions.state(now=NOW)
    [pos] = rows("SELECT * FROM positions")
    assert pos["qty"] == 0.1 and pos["entry_fees"] == 0 and pos["stop_price"] is None      # kept, upgraded in place
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
    decisions.state(now=NOW)
    [e] = events("RISK_SETTINGS_CHANGED")
    assert e["detail"]["changed"] == {"RISK_PER_TRADE_PCT": [0.01, 0.005]}


# S ----------------------------------------------------------------------------------------
def test_S_kill_switch_reset_needs_a_reviewed_server_side_approval():
    seed(cash=88_000.0, peak=100_000.0)
    run(Market(BTC_USD=view("BTC-USD", score=0.1)))
    from app.main import app
    with TestClient(app) as client:
        ks = client.get("/api/decisions").json()["risk"]["kill_switch"]
        assert ks["active"]
        url = "/api/risk/kill-switch/reset"
        good = {"reviewer": "Owner", "note": "Reviewed the 12% drawdown, accept it.", "confirm": decisions.RESET_PHRASE,
                "triggered_at": ks["triggered_at"]}
        assert client.post(url, json={}).status_code == 422
        for bad in ({"confirm": "reset"}, {"note": "ok"}, {"reviewer": " "}, {"triggered_at": "2026-01-01T00:00:00+00:00"}):
            res = client.post(url, json={**good, **bad})
            assert res.status_code == 409 and "NOT reset" in res.json()["detail"]
        client.post("/api/decisions/run")                                    # nothing else clears it
        assert client.get("/api/decisions").json()["risk"]["kill_switch"]["active"]
        res = client.post(url, json=good)
        assert res.status_code == 200 and not client.get("/api/decisions").json()["risk"]["kill_switch"]["active"]
    [e] = events("KILL_SWITCH_RESET")
    assert e["detail"]["reviewer"] == "Owner" and e["detail"]["old_peak"] == 100_000 and e["detail"]["new_peak"] == 88_000
    assert len(events("KILL_SWITCH_RESET_REFUSED")) == 4


def test_S_reset_refused_without_fresh_prices():
    seed("BTC-USD", 0.3, 50_000.0, cash=85_000.0, peak=112_000.0)
    run(Market(BTC_USD=view("BTC-USD", price=50_000.0, score=0.1)))
    ks = decisions.state(now=NOW)["risk"]["kill_switch"]
    ok, msg = decisions.reset_kill_switch("Owner", "Reviewed and accepted.", decisions.RESET_PHRASE, ks["triggered_at"],
                                          now=NOW + dt.timedelta(hours=1))
    assert not ok and "no fresh price for BTC-USD" in msg and decisions.state(now=NOW)["risk"]["kill_switch"]["active"]


# T U --------------------------------------------------------------------------------------
def test_T_nothing_fills_without_approval_and_the_old_loop_never_touches_the_ledger():
    p = run(Market(BTC_USD=view("BTC-USD")))["BTC-USD"]
    assert p["risk_decision"] == "RISK_APPROVED" and p["human_approval_status"] == "AWAITING_HUMAN_APPROVAL"
    assert rows("SELECT * FROM fills") == [] and rows("SELECT * FROM positions") == []
    floor = TradingFloor(seed=3)
    floor.broker.reset()
    floor.require_approval = False                                           # old demo loop auto-fills on its own account
    for _ in range(50):
        floor.run_cycle()
    assert rows("SELECT * FROM fills") == [] and len(rows("SELECT * FROM proposals")) == 1
    assert "decisions" not in open("app/engine.py", encoding="utf-8").read()


def test_U_parallel_approvals_and_parallel_stop_checks_execute_once():
    market = Market(BTC_USD=view("BTC-USD", price=50_000.0))
    p = run(market)["BTC-USD"]
    results = []
    threads = [threading.Thread(target=lambda: results.append(approve(p, market))) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(ok for ok, _ in results) == 1
    bars = [candle(0, 49_000, 49_500, 47_000, 47_200)]
    stops = []
    threads = [threading.Thread(target=lambda: stops.append(decisions.check_stops(NOW + dt.timedelta(hours=2), loader(bars))))
               for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(len(s) for s in stops) == 1 and len(rows("SELECT * FROM fills")) == 2


def test_U_approval_blocked_when_cash_or_positions_changed():
    market = Market(BTC_USD=view("BTC-USD"), ETH_USD=view("ETH-USD"))
    out = run(market)
    assert approve(out["BTC-USD"], market)[0]
    ok, msg = approve(out["ETH-USD"], market)                                # created before the BTC fill
    assert not ok and "paper cash or positions changed" in msg


# V ----------------------------------------------------------------------------------------
def test_V_abnormal_missing_or_stale_market_data_is_blocked():
    out = run(Market(BTC_USD=view("BTC-USD", price=200.0, prev=100.0), ETH_USD={**view("ETH-USD"), "price": float("nan")},
                     SOL_USD=view("SOL-USD", delayed=True), AAPL=view("AAPL", enough=False)))
    assert "abnormal price" in out["BTC-USD"]["risk_reason"] and "abnormal price" in out["ETH-USD"]["risk_reason"]
    assert "data freshness" in out["SOL-USD"]["risk_reason"] and out["AAPL"]["signal"] == "NO TRADE"
    assert rows("SELECT * FROM fills") == []


def test_V_stop_check_never_fills_on_bad_missing_or_delayed_candles():
    bought()
    bad = [candle(0, 47_000, 47_500, 46_000, 46_500, v=0), candle(2, 49_000, 49_500, 48_800, 49_100)]  # zero volume, gap
    out = decisions.check_stops(NOW + dt.timedelta(hours=4), loader(bad, delayed=True))
    [w] = out
    assert w["kind"] == "STOP_DATA_WARNING" and "zero/missing volume" in w["warning"] and "missing before" in w["warning"]
    assert "DELAYED" in w["warning"] and len(rows("SELECT * FROM fills")) == 1 and len(rows("SELECT * FROM positions")) == 1
    [w2] = decisions.check_stops(NOW + dt.timedelta(hours=5), loader(bad, delayed=True))           # bad candles already passed
    assert w2["warning"].startswith("DELAYED data") and "skipped" not in w2["warning"]
    assert decisions.check_stops(NOW + dt.timedelta(hours=6), loader(bad, delayed=True)) == []     # same warning: logged once


# W ----------------------------------------------------------------------------------------
def test_W_real_stored_data_path_still_works_and_feeds_the_stop_loss(market_db):
    out = {p["symbol"]: p for p in decisions.run(now=NOW, symbols=["SPY", "BTC-USD"])}
    assert out["SPY"]["stop_price"] == pytest.approx(139 * 0.95) and out["BTC-USD"]["risk_decision"] == "RISK_APPROVED"
    assert decisions.approve(out["BTC-USD"]["proposal_id"], out["BTC-USD"]["content_hash"], now=NOW)[0]
    rows_ = [[T0, "139.0", "140.0", "138.0", "139.0", "139.0", "5.0", 10], [T0 + H, "139.0", "139.0", "125.0", "126.0", "130.0", "8.0", 12]]
    market_db.replies["OHLC"] = {"error": [], "result": {"XXBTZUSD": rows_ + [[T0 + 2 * H, "126", "126", "126", "126", "126", "0.1", 1]],
                                                          "last": T0 + H}}
    later = NOW + dt.timedelta(hours=2, minutes=30)
    with __import__("contextlib").closing(cryptodata.connect()) as conn:
        assert cryptodata.fetch_kraken("BTC/USD", 60, conn, later)["status"] == "OK"
    [e] = decisions.check_stops(later)                                       # real loader on the stored candles
    assert e["kind"] == "STOP_LOSS" and e["fill_price"] == pytest.approx(139 * 0.95 * (1 - config.SLIPPAGE_PCT))
    from app.main import app
    with TestClient(app) as client:
        assert client.get("/api/stocks/SPY").json()["rows"] == 40 and client.get("/api/crypto/BTC-USD").json()["rows"] == 42
