"""Lesson 11 backtesting lab (SIMULATION / HISTORICAL BACKTEST). Test candles are hand-made and labeled TEST, or come from a
temp market DB; the real market DB, the live decision ledger and the paper account are never touched. No network, no Grok."""

import contextlib
import datetime as dt
import hashlib
import inspect
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app import backtest, config, cryptodata, decisions, stockdata, us_calendar

H = 3600
T0 = 1_700_000_000 // 86400 * 86400 - 37 * H        # candle 37 opens at 00:00 UTC (2023-11-15)
FAR = 2_000_000_000                                 # "now" for tests: every test candle is confirmed
FLAT = [100 + (0.2 if i % 2 else -0.2) for i in range(30)]
UP = [100 * 1.01 ** k for k in range(1, 8)]
DOWN = [UP[-1] * 0.997 ** k for k in range(1, 40)]


def cands(closes, start=T0, step=H, jump=0.0, vol=10.0, opens=None):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = (opens or {}).get(i, prev * (1 + jump))
        out.append(backtest._candle(start + i * step, step, o, max(o, c) * 1.001, min(o, c) * 0.999, c, vol, "TEST"))
        prev = c
    return out


def seg(candles, step=H, **cfg):
    c = backtest.normalize({"approval_policy": "SIMULATED_APPROVAL", "higher": [], **cfg})
    s = backtest.ListSource(candles, step, {})
    return backtest._segment("t", s, {}, candles[0]["open_time"], candles[-1]["open_time"], len(candles), c, FAR)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(decisions, "DB_PATH", tmp_path / "live_decisions.db")   # must never be created by a backtest


# 1 2 3 ------------------------------------------------------------------------------------
@pytest.fixture
def market(tmp_path, monkeypatch):
    """A temp Kraken table with 300 hourly BTC/USD candles (one 5-candle gap) read through the real SqlSource path."""
    monkeypatch.setattr(stockdata, "DB_PATH", tmp_path / "market.db")
    rows = [c for i, c in enumerate(cands(FLAT * 4 + UP + DOWN * 4 + FLAT * 2)) if not 150 <= i < 155]
    with contextlib.closing(cryptodata.connect()) as conn, conn:
        conn.executemany("INSERT INTO crypto_candles VALUES ('KRAKEN','SPOT','BTC/USD','XXBTZUSD','BTC','USD',60,?,?,?,?,?,NULL,?,5,1,'CSV','test',"
                         "'2026-11-15')", [(c["open_time"], c["open"], c["high"], c["low"], c["close"], c["volume"]) for c in rows])
    return tmp_path / "market.db"


def test_1_2_3_real_loader_split_and_gap_detection(market):
    before = hashlib.sha256(market.read_bytes()).hexdigest()
    r = backtest.run_backtest({"approval_policy": "SIMULATED_APPROVAL"}, now=FAR, store=False)
    assert r["status"] == "COMPLETE" and r["data"]["candles"] == len(FLAT * 4 + UP + DOWN * 4 + FLAT * 2) - 5
    assert r["data"]["provider"].startswith("Kraken") and "RAW" in r["data"]["price_type"] and r["data"]["first_open"]
    tr, te = r["train"], r["test"]
    assert tr["candles"] == int(r["data"]["candles"] * 0.7) and tr["candles"] + te["candles"] == r["data"]["candles"]
    assert tr["to"] <= te["from"] and te["warmup"]["to"] <= te["from"] and te["warmup"]["candles"] == backtest.WINDOW
    assert r["data"]["quality"] == "INCOMPLETE" and tr["data_quality"]["missing_candles"] == 5
    assert tr["data_quality"]["gaps"][0]["missing_candles"] == 5
    assert hashlib.sha256(market.read_bytes()).hexdigest() == before                   # source data unchanged (read-only)


def test_2_buy_right_after_a_gap_is_rejected():
    c = cands(FLAT + UP + DOWN)
    gapped = c[:30] + [{**x, "open_time": x["open_time"] + 3 * H, "close_time": x["close_time"] + 3 * H} for x in c[30:]]
    r = seg(gapped)
    assert r["data_quality"]["missing_candles"] == 3 and r["rejection_reasons"].get("data gap right before this candle") == 1


def test_insufficient_data_and_unsupported_prediction_markets():
    r = backtest.run_backtest({"source": "REAL", "market": "STOCK", "symbol": "SPY", "interval": 1440}, store=False)
    assert r["status"] == "INSUFFICIENT_DATA" and "Nothing was simulated" in r["reason"]
    assert backtest.run_backtest({"market": "PREDICTION"}, store=False)["status"] == "UNSUPPORTED"


# 4 5 --------------------------------------------------------------------------------------
def test_4_future_candles_never_change_past_decisions():
    base = cands(FLAT + UP + DOWN)
    future = base[:50] + [{**x, "open": x["open"] * 3, "high": x["high"] * 3, "low": x["low"] * 3, "close": x["close"] * 3,
                           "volume": 999.0} for x in base[50:]]
    a, b = seg(base), seg(future)
    cut = backtest._iso(base[50]["open_time"])
    assert [p for p in a["proposals"] if p["time"] <= cut] == [p for p in b["proposals"] if p["time"] <= cut]
    assert [x for x in a["chart"] if x["t"] <= cut] == [x for x in b["chart"] if x["t"] <= cut]
    assert "label_forward_return" not in inspect.getsource(backtest)


def test_5_higher_timeframe_uses_only_closed_candles():
    start = 1_700_006_400                                     # 2023-11-15 00:00 UTC
    hourly = cands([100 + i for i in range(40)], start=start)
    ten = start + 10 * H
    ctx = backtest._Higher(backtest.ListSource(hourly, H, {}), start, start + 40 * H, 60)
    at_1005 = ctx.at(ten + 300)                              # a 5-minute strategy deciding at 10:05
    assert at_1005 is None or at_1005["as_of"] == backtest._iso(ten)     # 10:00-11:00 candle is still open: not used
    later = cands([100 + i for i in range(40)], start=start)
    later[10] = {**later[10], "close": 1.0}                   # change the candle that is still open at 10:05
    ctx2 = backtest._Higher(backtest.ListSource(later, H, {}), start, start + 40 * H, 60)
    assert ctx2.at(ten + 300) == at_1005
    full = backtest._Higher(backtest.ListSource(hourly, H, {}), start, start + 40 * H, 60)
    assert full.at(start + 30 * H)["as_of"] == backtest._iso(start + 30 * H) and "trend" in full.at(start + 30 * H)


# 6 7 8 9 10 13 ----------------------------------------------------------------------------
def test_6_7_9_10_13_buy_and_sell_fill_at_the_next_open_with_fee_and_slippage():
    candles = cands(FLAT + UP + DOWN, jump=0.002)            # next open is 0.2% away from the signal close
    r = seg(candles)
    [t] = r["trades"]
    buy_i = next(i for i, c in enumerate(candles) if backtest._iso(c["close_time"]) == t["signal_time"])
    fee, slip = config.BACKTEST_COSTS["CRYPTO_SPOT"]["fee_pct"], config.BACKTEST_COSTS["CRYPTO_SPOT"]["slippage_pct"]
    nxt = candles[buy_i + 1]
    assert t["entry_time"] == backtest._iso(nxt["open_time"]) and t["entry_price"] == pytest.approx(nxt["open"] * (1 + slip))
    assert t["entry_price"] != pytest.approx(candles[buy_i]["close"])                     # never the signal close
    assert t["qty"] * candles[buy_i]["close"] <= 100_000 * config.MAX_POSITION_PCT + 1e-6  # 13: max position size
    assert t["exit_kind"] == "SIGNAL_SELL"
    exit_c = next(c for c in candles if backtest._iso(c["open_time"]) == t["exit_time"])
    assert t["exit_price"] == pytest.approx(exit_c["open"] * (1 - slip))
    entry_fee, exit_fee = t["qty"] * t["entry_price"] * fee, t["qty"] * t["exit_price"] * fee
    assert t["fees"] == pytest.approx(entry_fee + exit_fee)
    assert t["slippage_cost"] == pytest.approx(t["qty"] * (nxt["open"] + exit_c["open"]) * slip)
    assert t["pnl"] == pytest.approx((t["exit_price"] - t["entry_price"]) * t["qty"] - entry_fee - exit_fee)
    assert r["metrics"]["total_fees"] == pytest.approx(t["fees"], abs=0.01)                  # metrics are rounded to cents
    assert r["metrics"]["slippage_cost"] == pytest.approx(t["slippage_cost"], abs=0.01)


def test_8_hold_and_no_trade_create_no_orders():
    r = seg(cands(FLAT * 3))
    assert r["signals"].get("BUY", 0) == 0 and r["trades"] == [] and r["metrics"]["completed_trades"] == 0
    assert r["signals"]["NO TRADE"] == 90 and r["metrics"]["total_fees"] == 0
    held = seg(cands(FLAT + UP * 1 + [UP[-1] * 1.004 ** k for k in range(1, 6)]))
    assert held["signals"].get("HOLD", 0) > 0 and len(held["proposals"]) == 1               # HOLD makes no proposal


# 11 12 ------------------------------------------------------------------------------------
def test_11_stock_orders_fill_at_the_next_trading_session():
    days = us_calendar.trading_days(dt.date(2026, 10, 1), dt.date(2026, 12, 31))   # weekends + Thanksgiving absent
    closes = (FLAT + UP + DOWN)[:len(days)]
    candles = [{**c, "open_time": decisions._ts(str(d)), "close_time": decisions._ts(str(d)) + 86400}
               for c, d in zip(cands(closes, step=86400), days)]
    r = seg(candles, step=86400, market="STOCK", symbol="AAPL", interval=1440)
    assert r["data_quality"]["missing_candles"] == 0                                  # weekends / holidays are not gaps
    [t] = r["trades"]
    signal_day = dt.date.fromisoformat(t["signal_time"][:10]) - dt.timedelta(days=1)   # bar date (close stamped at midnight after)
    entry_day = dt.date.fromisoformat(t["entry_time"][:10])
    assert us_calendar.is_trading_day(entry_day) and entry_day == next(d for d in days if d > signal_day)
    assert t["qty"] == int(t["qty"])                                                  # whole shares


def test_12_crypto_trades_on_weekends():
    sat = dt.datetime(2023, 11, 18, tzinfo=dt.timezone.utc).timestamp() - 30 * H     # BUY signal candle closes Saturday 00:00
    r = seg(cands(FLAT + UP + DOWN, start=int(sat)))
    entry = dt.datetime.fromisoformat(r["trades"][0]["entry_time"].replace("Z", "+00:00"))
    assert entry.weekday() == 5                                                        # filled on Saturday, no session wait


# 14 15 16 17 ------------------------------------------------------------------------------
def crash(gap):
    """BUY, then a candle opening `gap` below (through the stop), then a flat base and a new up-move the same UTC day."""
    closes = FLAT + UP + [UP[-1] * (1 - gap)] + [UP[-1] * (1 - gap) + (0.05 if i % 2 else -0.05) for i in range(20)] + \
        [UP[-1] * (1 - gap) * 1.01 ** k for k in range(1, 5)]
    return cands(closes, opens={len(FLAT + UP): UP[-1] * (1 - gap)})


def test_14_15_daily_loss_limit_after_a_gap_down_stop():
    r = seg(crash(0.25))
    t = r["trades"][0]
    gap_open = UP[-1] * 0.75
    assert t["exit_kind"] == "STOP_LOSS_GAP" and t["exit_price"] == pytest.approx(gap_open * (1 - backtest.normalize({})["slippage_pct"]))
    assert t["exit_price"] < t["stop"]                                                # not filled at the stop price
    assert r["rejection_reasons"].get("Daily loss limit hit", 0) >= 1 or any("Daily loss limit hit" in p["reason"] for p in r["proposals"])
    assert "KILL_SWITCH_TRIGGERED" not in r["event_counts"]


def test_15_stop_and_take_profit_in_one_candle_assumes_the_stop():
    candles = cands(FLAT + UP + DOWN)
    i = len(FLAT) + 2
    o = candles[i]["open"]
    candles[i] = {**candles[i], "high": o * 1.2, "low": o * 0.9}                       # both levels inside one candle
    t = seg(candles)["trades"][0]
    assert t["exit_kind"] == "STOP_LOSS_AMBIGUOUS" and t["pnl"] < 0


def test_16_17_kill_switch_triggers_and_blocks_new_buys():
    r = seg(crash(0.75))
    assert r["event_counts"]["KILL_SWITCH_TRIGGERED"] == 1 and r["metrics"]["max_drawdown_pct"] >= 10
    assert r["rejection_reasons"].get("drawdown kill switch active", 0) >= 1
    assert all(p["risk_decision"] == "RISK_REJECTED" for p in r["proposals"][1:] if p["signal"] == "BUY")


def test_17_abnormal_jump_is_rejected_and_proposal_only_never_fills():
    closes = FLAT + [100 * 1.5] + [150 * 1.01 ** k for k in range(1, 10)]
    r = seg(cands(closes), approval_policy="PROPOSAL_ONLY")
    assert r["trades"] == [] and r["metrics"]["completed_trades"] == 0 and not any(e["kind"] == "NO_FILL" for e in r["events"])
    assert all(p["approval"] != "SIMULATED_APPROVAL (backtest policy, not a human)" for p in r["proposals"])


# 18 19 ------------------------------------------------------------------------------------
def test_18_19_backtest_approval_never_reaches_the_live_path_or_the_network(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or pytest.fail("network call"))
    r = backtest.run_backtest({"source": "DEMO", "approval_policy": "SIMULATED_APPROVAL"}, store=False)
    assert r["status"] == "COMPLETE" and r["train"]["trades"] and calls == []
    assert not decisions.DB_PATH.exists()                                             # live ledger never opened
    src = inspect.getsource(backtest)
    assert "decisions.approve" not in src and "decisions.run(" not in src and "_fill(" not in src and "urlopen" not in src
    assert "SIMULATED_APPROVAL" not in inspect.getsource(decisions)
    with pytest.raises(ValueError):
        backtest.normalize({"approval_policy": "HUMAN_APPROVED"})
    assert backtest.normalize({})["approval_policy"] == "PROPOSAL_ONLY"               # simulated approval only when chosen


# 20 21 22 23 24 ---------------------------------------------------------------------------
def test_20_21_23_24_return_win_rate_drawdown_and_buy_and_hold():
    candles = cands(FLAT + UP + DOWN + FLAT + UP + DOWN, jump=0.001)
    r = seg(candles)
    m, trades = r["metrics"], r["trades"]
    assert m["ending_equity"] == pytest.approx(100_000 + sum(t["pnl"] for t in trades), abs=0.01)
    assert m["total_return_pct"] == pytest.approx((m["ending_equity"] / 100_000 - 1) * 100, abs=1e-3)
    wins = [t for t in trades if t["pnl"] > 0]
    assert m["win_rate_pct"] == round(len(wins) / len(trades) * 100, 1)
    peak, mdd = 100_000.0, 0.0
    for p in r["chart"]:                                                              # chart = every candle here (small run)
        peak = max(peak, p["eq"])
        mdd = max(mdd, (peak - p["eq"]) / peak * 100)
    assert m["max_drawdown_pct"] == pytest.approx(mdd, abs=1e-3)
    fee, slip = backtest.normalize({})["fee_pct"], backtest.normalize({})["slippage_pct"]
    qty = 100_000 / (candles[0]["open"] * (1 + slip) * (1 + fee))
    assert r["benchmark"]["return_pct"] == pytest.approx((qty * candles[-1]["close"] / 100_000 - 1) * 100, abs=1e-3)
    assert r["comparison"]["excess_return_pct"] == pytest.approx(m["total_return_pct"] - r["benchmark"]["return_pct"], abs=1e-3)


def test_22_profit_factor_is_na_when_undefined():
    none = seg(cands(FLAT * 3))["metrics"]
    assert none["profit_factor"] == "N/A" and none["win_rate_pct"] == "N/A" and none["profit_factor_note"] == "no completed trades"
    one_win = seg(cands(FLAT + UP + [UP[-1] * 1.01 ** k for k in range(1, 12)]))["metrics"]
    assert one_win["completed_trades"] >= 1 and one_win["profit_factor"] == "N/A" and "no losing trades" in one_win["profit_factor_note"]


# storage + API ----------------------------------------------------------------------------
def test_runs_are_stored_reproducibly_and_interrupted_runs_are_marked():
    r = backtest.run_backtest({"source": "DEMO", "approval_policy": "SIMULATED_APPROVAL"})
    stored = backtest.get_run(r["run_id"])
    assert stored["status"] == "COMPLETE" and stored["label"].endswith("DEMO DATA (synthetic, not real)")
    assert stored["code_version"] == backtest.code_version() and stored["config"]["fee_pct"] == config.BACKTEST_COSTS["CRYPTO_SPOT"]["fee_pct"]
    assert (backtest.RUNS_DIR / r["run_id"] / "config.json").exists() and not list(backtest.RUNS_DIR.rglob("*.tmp"))
    backtest._write("aaaaaaaaaaaa", "result.json", {"run_id": "aaaaaaaaaaaa", "status": "RUNNING", "config": {}})
    assert backtest.get_run("aaaaaaaaaaaa")["status"] == "INTERRUPTED"
    assert backtest.get_run("../../etc/x") is None and {x["run_id"] for x in backtest.list_runs()} >= {r["run_id"], "aaaaaaaaaaaa"}


def test_backtest_api():
    from app.main import app
    with TestClient(app) as client:
        opts = client.get("/api/backtest/options").json()
        assert opts["label"] == "SIMULATION / HISTORICAL BACKTEST" and "SIMULATED_APPROVAL" in opts["policies"]
        assert client.post("/api/backtest/run", json={"approval_policy": "LIVE"}).status_code == 400
        run_id = client.post("/api/backtest/run", json={"source": "DEMO", "approval_policy": "SIMULATED_APPROVAL"}).json()["run_id"]
        for _ in range(100):
            r = client.get(f"/api/backtest/runs/{run_id}").json()
            if r["status"] != "RUNNING":
                break
            time.sleep(0.05)
        assert r["status"] == "COMPLETE" and "DEMO" in r["label"]
        assert client.get("/api/backtest/runs").json()["runs"][0]["run_id"] == run_id
        assert client.get("/api/backtest/runs/zzz").status_code == 404
