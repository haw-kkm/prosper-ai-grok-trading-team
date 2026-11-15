"""Real stock data: collection, validation, storage, indicators, dashboard API and safety.
Every Alpha Vantage / xAI call is faked: these tests never reach the internet."""

import contextlib
import datetime as dt
import inspect
import io
import json
import pathlib
import re
import sqlite3
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import config, engine, indicators, stockdata, us_calendar
from app.agents import market_analyst
from app.engine import TradingFloor
from tests.test_grok import FakeResponse, chat, xai  # noqa: F401  (xai is a fixture)

NOW = dt.datetime(2026, 11, 16, 0, 0, tzinfo=dt.timezone.utc)  # Sunday; last completed session = Fri 2026-11-13
AV_KEY = "AVTESTKEY1234567890"


def trading_dates(n, end=dt.date(2026, 11, 13)):
    return us_calendar.trading_days(end - dt.timedelta(days=n * 2 + 10), end)[-n:]


def raw_rows(n=40, start=100.0, step=1.0):
    return [{"ts": d.isoformat(), "open": str(start + i * step - 0.5), "high": str(start + i * step + 1),
             "low": str(start + i * step - 1), "close": str(start + i * step), "volume": str(1000 + i)}
            for i, d in enumerate(trading_dates(n))]


def write_csv(path, rows, extra=None):
    extra = extra or {}
    head = ["Date", "Open", "High", "Low", "Close", "Volume", *extra]
    lines = [",".join(head)] + [",".join([r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"], *extra.values()])
                                for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def av_payload(rows, symbol="SPY"):
    return {"Meta Data": {"1. Information": "Daily Prices (open, high, low, close) and Volumes", "2. Symbol": symbol,
                          "3. Last Refreshed": rows[-1]["ts"], "4. Output Size": "Compact", "5. Time Zone": "US/Eastern"},
            "Time Series (Daily)": {r["ts"]: {"1. open": r["open"], "2. high": r["high"], "3. low": r["low"],
                                              "4. close": r["close"], "5. volume": r["volume"]} for r in reversed(rows)}}


def count(conn, symbol="SPY"):
    return conn.execute("SELECT COUNT(*) FROM bars WHERE symbol=?", (symbol,)).fetchone()[0]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stockdata, "DB_PATH", tmp_path / "market.db")
    with contextlib.closing(stockdata.connect()) as conn:
        yield conn


@pytest.fixture
def av(monkeypatch):
    """Alpha Vantage 'on' with a fake key. Each request gets `av.reply`; URLs are recorded in `av.calls`."""
    monkeypatch.setattr(config, "ALPHAVANTAGE_API_KEY", AV_KEY)

    class Fake:
        reply, calls = None, []

    def fake_urlopen(url, timeout):
        Fake.calls.append(url)
        if isinstance(Fake.reply, BaseException):
            raise Fake.reply
        return FakeResponse(Fake.reply if isinstance(Fake.reply, bytes) else json.dumps(Fake.reply).encode())

    monkeypatch.setattr(stockdata.urllib.request, "urlopen", fake_urlopen)
    return Fake


# ---------- US market calendar ----------
def test_us_market_holidays_and_sessions():
    d = dt.date
    assert us_calendar.holidays(2025) == {d(2025, 1, 1), d(2025, 1, 9), d(2025, 1, 20), d(2025, 2, 17), d(2025, 4, 18),
                                          d(2025, 5, 26), d(2025, 6, 19), d(2025, 7, 4), d(2025, 9, 1), d(2025, 11, 27), d(2025, 12, 25)}
    assert us_calendar.holidays(2026) == {d(2026, 1, 1), d(2026, 1, 19), d(2026, 2, 16), d(2026, 4, 3), d(2026, 5, 25),
                                          d(2026, 6, 19), d(2026, 7, 3), d(2026, 9, 7), d(2026, 11, 26), d(2026, 12, 25)}
    assert d(2024, 3, 29) in us_calendar.holidays(2024)                 # Good Friday 2024
    assert us_calendar.is_trading_day(d(2021, 12, 31))                   # Saturday New Year's is not moved back
    assert d(2022, 6, 20) in us_calendar.holidays(2022)                  # Sunday Juneteenth -> Monday
    assert not us_calendar.is_trading_day(d(2026, 11, 14))               # Saturday
    utc = dt.timezone.utc
    assert not us_calendar.bar_is_final(d(2026, 11, 13), dt.datetime(2026, 11, 13, 21, 59, tzinfo=utc))
    assert us_calendar.bar_is_final(d(2026, 11, 13), dt.datetime(2026, 11, 13, 22, 0, tzinfo=utc))
    assert us_calendar.last_completed_session(NOW) == d(2026, 11, 13)
    assert us_calendar.last_completed_session(dt.datetime(2026, 11, 26, 23, 0, tzinfo=utc)) == d(2026, 11, 25)  # Thanksgiving


# ---------- indicators ----------
def bars_from(closes, volumes=None):
    dates = trading_dates(len(closes))
    return [{"ts": day.isoformat(), "close": c, "volume": (volumes or [100] * len(closes))[i]}
            for i, (day, c) in enumerate(zip(dates, closes))]


def test_sma_momentum_volatility_and_volume_change_known_values():
    closes = [float(i) for i in range(1, 31)]
    a = market_analyst.analyze_bars("SPY", bars_from(closes, [100] * 29 + [200]))
    assert a["status"] == "OK" and a["as_of"] == "2026-11-13"
    assert a["sma_fast"] == 28.0 and a["sma_slow"] == 20.5            # mean(26..30), mean(11..30)
    assert a["momentum_pct"] == round((30 / 11 - 1) * 100, 3)          # close vs 19 sessions earlier, as run() does
    assert a["volatility_pct"] > 0 and a["trend"] == "up"
    assert a["volume_change_pct"] == 100.0                             # 200 vs the 20-session average of 100
    assert a["rsi_14"] == 100.0 and a["macd"] == indicators.INSUFFICIENT  # MACD needs 34 bars; only 30 given
    assert indicators.macd([float(i) for i in range(1, 41)])["macd"] > 0   # rising prices -> positive MACD


def test_insufficient_data_is_reported_not_invented():
    a = market_analyst.analyze_bars("SPY", bars_from([float(i) for i in range(1, 25)]))
    assert a["status"] == "INSUFFICIENT DATA" and "sma_fast" not in a and a["needed"] == 25
    assert market_analyst.analyze_bars("SPY", [])["status"] == "INSUFFICIENT DATA"
    assert indicators.rsi([1.0] * 14) == indicators.INSUFFICIENT
    assert indicators.macd([float(i) for i in range(33)]) == indicators.INSUFFICIENT
    assert isinstance(indicators.macd([float(i) for i in range(34)]), dict)
    assert indicators.rsi([5.0] * 20) == 50.0 and indicators.rsi([float(30 - i) for i in range(20)]) == 0.0


def test_indicators_never_use_future_bars():
    closes = [100 + ((i * 37) % 11) - i * 0.3 for i in range(80)]
    full_sma, full_ema = indicators.sma_series(closes, 20), indicators.ema_series(closes, 12)
    for i in range(len(closes)):
        assert full_sma[i] == indicators.sma_series(closes[:i + 1], 20)[i]
        assert full_ema[i] == indicators.ema_series(closes[:i + 1], 12)[i]
    early = market_analyst.analyze_bars("SPY", bars_from(closes)[:40])
    assert early == market_analyst.analyze_bars("SPY", bars_from(closes)[:40])  # same cut-off -> same answer
    assert indicators.rsi(closes[:40]) == early["rsi_14"] and indicators.macd(closes[:40]) == early["macd"]


# ---------- CSV import, validation, duplicates, storage ----------
def test_csv_import_without_api_key_is_marked_unverified(db, tmp_path):
    assert config.ALPHAVANTAGE_API_KEY == ""
    result = stockdata.import_csv(write_csv(tmp_path / "spy.csv", raw_rows()), db, symbol="SPY", now_utc=NOW)
    assert result["inserted"] == 40 and result["rejected"] == 0 and "not stated" in result["message"]
    row = db.execute("SELECT provider, origin, data_type, source FROM bars WHERE symbol='SPY' LIMIT 1").fetchone()
    assert tuple(row) == ("UNKNOWN", "CSV", "RAW", "csv:spy.csv")
    detail = stockdata.detail("SPY", NOW)
    assert detail["status"] == "CSV · UNVERIFIED · SOURCE UNKNOWN"
    assert any("source UNKNOWN" in w for w in detail["warnings"])
    named = stockdata.import_csv(write_csv(tmp_path / "qqq.csv", raw_rows(), {"Symbol": "QQQ", "Provider": "Nasdaq"}), db, now_utc=NOW)
    assert named["inserted"] == 40 and stockdata.detail("QQQ", NOW)["status"] == "CSV · UNVERIFIED"


def test_missing_values_and_abnormal_prices_are_rejected(db, tmp_path):
    rows = raw_rows(30)
    bad = [
        {**rows[0], "close": ""},                                   # missing close
        {**rows[1], "volume": "abc"},                               # not a number
        {**rows[2], "volume": "-5"},                                # negative volume
        {**rows[3], "high": "10", "low": "20"},                     # high below low
        {**rows[4], "open": "0"},                                   # zero price
        {**rows[5], "close": "nan"},                                # not finite
        {**rows[6], "ts": "2026-11-14"},                            # Saturday
        {**rows[7], "ts": "2027-01-04"},                            # future
        {**rows[8], "ts": "13/11/2026"},                            # unreadable date
    ]
    result = stockdata.import_csv(write_csv(tmp_path / "bad.csv", bad + rows[9:]), db, symbol="SPY", now_utc=NOW)
    assert result["rejected"] == 9 and result["inserted"] == 21 and result["status"] == "PARTIAL"
    for reason in ("missing close", "not a number", "negative volume", "OHLC out of order", "above 0",
                   "finite", "weekend", "future", "bad date"):
        assert reason in result["message"]
    assert count(db) == 21
    with pytest.raises(sqlite3.IntegrityError):  # the schema itself also refuses broken bars
        db.execute("INSERT INTO bars VALUES ('stock','SPY','daily','2026-01-05','RAW',10,5,8,9,100,'X','CSV','t','t')")


def test_duplicates_are_never_overwritten(db, tmp_path):
    rows = raw_rows()
    path = write_csv(tmp_path / "a.csv", rows)
    stockdata.import_csv(path, db, symbol="SPY", now_utc=NOW)
    again = stockdata.import_csv(path, db, symbol="SPY", now_utc=NOW)
    assert again["inserted"] == 0 and again["unchanged"] == 40 and again["status"] == "OK"
    changed = [dict(r) for r in rows]
    changed[5]["close"] = str(float(changed[5]["close"]) + 0.25)
    clash = stockdata.import_csv(write_csv(tmp_path / "b.csv", changed + [changed[6]]), db, symbol="SPY", now_utc=NOW)
    assert clash["conflicts"] == 1 and clash["rejected"] == 1 and "same date twice" in clash["message"]
    kept = db.execute("SELECT close, source FROM bars WHERE symbol='SPY' AND ts=?", (rows[5]["ts"],)).fetchone()
    assert kept["close"] == float(rows[5]["close"]) and kept["source"] == "csv:a.csv"  # original value and source kept


def test_unfinished_session_is_not_stored(db, tmp_path):
    friday_midday = dt.datetime(2026, 11, 13, 18, 0, tzinfo=dt.timezone.utc)
    result = stockdata.import_csv(write_csv(tmp_path / "s.csv", raw_rows(30)), db, symbol="SPY", now_utc=friday_midday)
    assert result["incomplete"] == 1 and result["inserted"] == 29
    assert stockdata.latest_date(db, "SPY", "RAW") == "2026-11-12"


def test_gaps_and_possible_splits_are_flagged(db, tmp_path):
    rows = raw_rows(40)
    del rows[10]                                                   # a missing trading day
    rows[30] = {**rows[30], "open": "300", "high": "301", "low": "299", "close": "300"}  # +~120% jump
    stockdata.import_csv(write_csv(tmp_path / "g.csv", rows), db, symbol="SPY", now_utc=NOW)
    text = " ".join(stockdata.detail("SPY", NOW)["warnings"])
    assert "1 trading day(s) missing" in text and "Possible stock split" in text


def test_database_survives_restart_and_failed_batches_roll_back(db, tmp_path, monkeypatch):
    stockdata.import_csv(write_csv(tmp_path / "a.csv", raw_rows(30)), db, symbol="SPY", now_utc=NOW)
    db.close()
    with contextlib.closing(stockdata.connect()) as reopened:      # "restart": a brand-new connection
        assert count(reopened) == 30
        real_store = stockdata._store

        def store_then_crash(conn, *args):
            real_store(conn, *args)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(stockdata, "_store", store_then_crash)
        result = stockdata.import_csv(write_csv(tmp_path / "q.csv", raw_rows(30)), reopened, symbol="QQQ", now_utc=NOW)
        assert result["status"] == "ERROR" and "nothing from this batch was stored" in result["message"]
        assert count(reopened, "QQQ") == 0 and count(reopened) == 30  # no half-written batch, old data intact
        assert reopened.execute("PRAGMA quick_check").fetchone()[0] == "ok"


# ---------- Alpha Vantage (mocked) ----------
def test_alpha_vantage_fetch_parses_validates_and_stores(db, av):
    rows = raw_rows(40)
    rows[3] = {**rows[3], "volume": "-1"}
    av.reply = av_payload(rows)
    result = stockdata.fetch_alpha_vantage("spy", db, now_utc=NOW)
    assert result["request_sent"] and result["inserted"] == 39 and result["rejected"] == 1 and result["status"] == "PARTIAL"
    url = av.calls[0]
    assert "function=TIME_SERIES_DAILY&" in url and "outputsize=compact" in url and "symbol=SPY" in url
    assert "full" not in url and "ADJUSTED" not in url
    row = db.execute("SELECT provider, origin, data_type FROM bars WHERE symbol='SPY' LIMIT 1").fetchone()
    assert tuple(row) == ("ALPHA_VANTAGE", "API", "RAW")
    assert stockdata.detail("SPY", NOW)["status"] == "REAL DATA · ALPHA VANTAGE"
    assert AV_KEY not in json.dumps(result) and AV_KEY not in json.dumps(stockdata.recent_log(db, "SPY"))


@pytest.mark.parametrize("reply, kind", [
    ({"Error Message": "the parameter apikey is invalid or missing. Please claim your free API key."}, "auth"),
    ({"Error Message": "Invalid API call. Please retry or visit the documentation for TIME_SERIES_DAILY."}, "invalid_request"),
    ({"Information": f"We have detected your API key as {AV_KEY} and our standard API rate limit is 25 requests per day."}, "rate_limit"),
    ({"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute."}, "rate_limit"),
    ({"Information": "Thank you for using Alpha Vantage! This is a premium endpoint."}, "premium"),
    (urllib.error.HTTPError("https://www.alphavantage.co/query", 503, "down", {}, io.BytesIO(b"")), "http"),
    (urllib.error.URLError(ConnectionResetError()), "network"),
    (TimeoutError(), "timeout"),
    (b"<html>maintenance</html>", "bad_response"),
    (av_payload(raw_rows(5), symbol="QQQ"), "bad_response"),       # answer for the wrong symbol
])
def test_alpha_vantage_errors_store_nothing_and_never_retry(db, av, reply, kind, caplog):
    av.reply = reply
    result = stockdata.fetch_alpha_vantage("SPY", db, now_utc=NOW)
    assert result["status"] == "ERROR" and result["message"].startswith(kind) and result["request_sent"]
    assert len(av.calls) == 1 and count(db) == 0
    everything = json.dumps(result) + json.dumps(stockdata.recent_log(db, "SPY")) + caplog.text
    assert AV_KEY not in everything


def test_no_key_means_no_request_and_data_unavailable(db, monkeypatch):
    monkeypatch.setattr(stockdata.urllib.request, "urlopen", lambda *a, **k: pytest.fail("request without a key"))
    result = stockdata.fetch_alpha_vantage("SPY", db, now_utc=NOW)
    assert result["status"] == "SKIPPED" and not result["request_sent"] and "DATA UNAVAILABLE" in result["message"]
    assert stockdata.detail("SPY", NOW)["status"] == "DATA UNAVAILABLE"


def test_daily_limit_and_up_to_date_skip_save_requests(db, av, tmp_path):
    with db:
        for _ in range(stockdata.AV_DAILY_LIMIT):
            db.execute("INSERT INTO ingest_log (time, asset_class, symbol, provider, origin, source, request_sent, status) "
                       "VALUES (?, 'stock', 'QQQ', 'ALPHA_VANTAGE', 'API', 's', 1, 'OK')", (NOW.isoformat(),))
    assert "limit reached" in stockdata.fetch_alpha_vantage("SPY", db, now_utc=NOW)["message"]
    db.execute("DELETE FROM ingest_log")
    stockdata.import_csv(write_csv(tmp_path / "a.csv", raw_rows()), db, symbol="SPY", now_utc=NOW)  # through 2026-11-13
    assert "up to date" in stockdata.fetch_alpha_vantage("SPY", db, now_utc=NOW)["message"]
    assert av.calls == []


# ---------- Grok interface for real indicators (mocked) ----------
def test_grok_research_interface_for_real_indicators(xai):  # noqa: F811
    analysis = market_analyst.analyze_bars("SPY", bars_from([float(i) for i in range(1, 41)]))
    xai.reply = chat({"analyses": [{"symbol": "SPY", "view": "bullish", "confidence": 0.7, "rationale": "SMA5 above SMA20."}],
                      "summary": "Uptrend."})
    research = market_analyst.research_real([analysis], "Historical daily bars (CSV, provider UNKNOWN, RAW) up to 2026-11-13. Not real-time.")
    sent = json.loads(xai.calls[0]["body"]["messages"][1]["content"])
    assert sent["data_source"].startswith("Historical daily bars") and sent["symbols"][0]["rsi_14"] == 100.0
    assert sent["symbols"][0]["as_of"] == "2026-11-13" and research["analyses"]["SPY"]["view"] == "bullish"


# ---------- dashboard API ----------
def test_stock_dashboard_api(db, tmp_path):
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        overview = client.get("/api/stocks").json()
        assert [s["symbol"] for s in overview["symbols"]] == list(stockdata.SYMBOLS)
        assert all(s["status"] == "DATA UNAVAILABLE" for s in overview["symbols"]) and overview["api_key_set"] is False
        stockdata.import_csv(write_csv(tmp_path / "a.csv", raw_rows(60)), db, symbol="SPY", now_utc=NOW)
        spy = client.get("/api/stocks/spy").json()
        assert spy["rows"] == 60 and len(spy["bars"]) == len(spy["sma5"]) == len(spy["sma20"]) == 60
        assert spy["sma5"][3] is None and spy["sma20"][19] is not None and spy["analysis"]["status"] == "OK"
        assert spy["status"].startswith("CSV") and "not real-time" in spy["note"]
        assert client.get("/api/stocks/AAPL").json()["status"] == "DATA UNAVAILABLE"
        assert client.get("/api/stocks/BTC-USD").status_code == 404


# ---------- safety ----------
def test_real_stock_data_never_reaches_the_trading_simulation(db, tmp_path):
    rows = raw_rows(40, start=5000.0)                                 # absurd SPY prices, easy to spot
    stockdata.import_csv(write_csv(tmp_path / "a.csv", rows), db, symbol="SPY", now_utc=NOW)
    floor = TradingFloor(seed=4)
    floor.broker.reset()  # the test paper account is shared between tests
    for _ in range(3):
        floor.run_cycle()
    assert floor.market.prices["SPY"] < 2000 and "stockdata" not in inspect.getsource(engine)
    assert all(t["price"] < 2000 for t in floor.broker.trades if t["symbol"] == "SPY")


def test_no_real_trading_code_and_only_known_hosts():
    hosts, broker_libs = set(), []
    for path in pathlib.Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hosts |= set(re.findall(r"https?://([A-Za-z0-9.\-]+)", text))
        broker_libs += re.findall(r"^\s*(?:import|from)\s+(alpaca|ccxt|ib_insync|binance|robin_stocks|schwab|tda)\b", text, re.M)
    assert hosts <= {"api.x.ai", "docs.x.ai", "www.alphavantage.co", "api.kraken.com", "127.0.0.1"}, hosts
    assert broker_libs == []
    assert "def buy" in inspect.getsource(__import__("app.broker", fromlist=["PaperBroker"]).PaperBroker)  # paper only


def test_server_start_makes_no_paid_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "XAI_API_KEY", "xai-FAKEFAKEFAKEFAKE")
    monkeypatch.setattr(config, "ALPHAVANTAGE_API_KEY", AV_KEY)
    monkeypatch.setattr(stockdata.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or pytest.fail("network call"))
    from app.main import app, floor
    with TestClient(app) as client:
        for _ in range(3):
            floor.run_cycle()
        for url in ("/api/state", "/api/health", "/api/stocks", "/api/stocks/SPY", "/api/crypto", "/api/crypto/BTC-USD"):
            assert client.get(url).status_code == 200
    assert calls == []
