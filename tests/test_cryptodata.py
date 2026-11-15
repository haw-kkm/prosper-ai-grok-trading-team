"""Kraken spot crypto data: pair mapping, OHLC parsing, validation, storage, gaps, indicators, Market Analyst,
dashboard API and safety. Every Kraken / xAI call is faked: these tests never reach the internet."""

import contextlib
import datetime as dt
import inspect
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import cryptodata, engine, indicators, stockdata
from app.agents import market_analyst
from app.engine import TradingFloor
from tests.test_grok import FakeResponse, chat, xai  # noqa: F401  (xai is a fixture)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 16, 0, 30, tzinfo=UTC)         # last closed 60-min candle opened 2026-11-15 23:00
H = 3600
LAST = int(dt.datetime(2026, 11, 15, 23, 0, tzinfo=UTC).timestamp())
ASSET_PAIRS = {"error": [], "result": {
    "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD", "status": "online"},
    "XETHZUSD": {"altname": "ETHUSD", "wsname": "ETH/USD", "base": "XETH", "quote": "ZUSD", "status": "online"},
    "SOLUSD": {"altname": "SOLUSD", "wsname": "SOL/USD", "base": "SOL", "quote": "ZUSD", "status": "online"},
}}


def candles(n=40, end=LAST, start=100.0):
    """Kraken-format confirmed candles [time, o, h, l, c, vwap, volume, count], oldest first."""
    return [[end - (n - 1 - i) * H, f"{start + i - 0.5:.1f}", f"{start + i + 1:.1f}", f"{start + i - 1:.1f}",
             f"{start + i:.1f}", f"{start + i:.2f}", f"{10 + i:.8f}", 5 + i] for i in range(n)]


def ohlc(rows, key="XXBTZUSD"):
    """OHLC answer: the rows plus Kraken's still-open current candle as the last entry."""
    current = [rows[-1][0] + H, rows[-1][4], rows[-1][4], rows[-1][4], rows[-1][4], rows[-1][4], "0.1", 1] if rows else []
    return {"error": [], "result": {key: rows + ([current] if current else []), "last": rows[-1][0] if rows else 0}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stockdata, "DB_PATH", tmp_path / "market.db")
    with contextlib.closing(cryptodata.connect()) as conn:
        yield conn


@pytest.fixture
def kraken(monkeypatch):
    """Answers per endpoint from `kraken.replies`; every URL is recorded in `kraken.calls`."""
    class Fake:
        replies, calls = {"AssetPairs": ASSET_PAIRS}, []

        @classmethod
        def count(cls, endpoint):
            return sum(f"/0/public/{endpoint}?" in u for u in cls.calls)

    def fake_urlopen(url, timeout):
        Fake.calls.append(url)
        reply = Fake.replies[url.split("/0/public/")[1].split("?")[0]]
        if isinstance(reply, BaseException):
            raise reply
        return FakeResponse(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

    monkeypatch.setattr(cryptodata.urllib.request, "urlopen", fake_urlopen)
    return Fake


def count(conn, pair="BTC/USD", interval=60):
    return conn.execute("SELECT COUNT(*) FROM crypto_candles WHERE pair=? AND interval_min=?", (pair, interval)).fetchone()[0]


# ---------- pair mapping ----------
def test_pairs_are_mapped_from_kraken_asset_pairs_and_cached(db, kraken):
    mapping, sent = cryptodata.pair_map(db, NOW)
    assert sent == 1 and "pair=BTC%2FUSD%2CETH%2FUSD%2CSOL%2FUSD" in kraken.calls[0]
    assert (mapping["BTC/USD"]["exchange_pair"], mapping["BTC/USD"]["altname"]) == ("XXBTZUSD", "XBTUSD")
    assert (mapping["ETH/USD"]["exchange_pair"], mapping["ETH/USD"]["altname"]) == ("XETHZUSD", "ETHUSD")
    assert (mapping["SOL/USD"]["exchange_pair"], mapping["SOL/USD"]["base"], mapping["SOL/USD"]["quote"]) == ("SOLUSD", "SOL", "USD")
    assert cryptodata.pair_map(db, NOW + dt.timedelta(days=1))[1] == 0          # cached: no new request


def test_display_name_asset_pairs_keys_match_the_ohlc_pair_id(db, kraken):
    # Real Kraken behaviour seen on 2026-11-15: AssetPairs queried by "BTC/USD" answers under "BTC/USD",
    # while OHLC (queried by altname XBTUSD) answers under "XXBTZUSD".
    kraken.replies["AssetPairs"] = {"error": [], "result": {
        "BTC/USD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD", "status": "online"},
        "ETH/USD": {"altname": "ETHUSD", "wsname": "ETH/USD", "base": "XETH", "quote": "ZUSD", "status": "online"},
        "SOL/USD": {"altname": "SOLUSD", "wsname": "SOL/USD", "base": "SOL", "quote": "ZUSD", "status": "online"}}}
    kraken.replies["OHLC"] = ohlc(candles(30), key="XXBTZUSD")
    result = cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    assert result["status"] == "OK" and result["inserted"] == 30
    assert db.execute("SELECT exchange_pair FROM crypto_pairs WHERE pair='BTC/USD'").fetchone()[0] == "XXBTZUSD"
    assert db.execute("SELECT DISTINCT exchange_pair FROM crypto_candles").fetchone()[0] == "XXBTZUSD"
    kraken.replies["OHLC"] = ohlc(candles(30), key="XETHZUSD")              # wrong pair for BTC: refused
    assert cryptodata.fetch_kraken("SOL/USD", 60, db, NOW)["message"].startswith("bad_response")


def test_a_pair_kraken_does_not_list_is_never_invented(db, kraken):
    kraken.replies["AssetPairs"] = {"error": [], "result": {k: v for k, v in ASSET_PAIRS["result"].items() if k != "SOLUSD"}}
    result = cryptodata.fetch_kraken("SOL/USD", 60, db, NOW)
    assert result["status"] == "ERROR" and "does not list SOL/USD" in result["message"]
    assert kraken.count("OHLC") == 0 and count(db, "SOL/USD") == 0


# ---------- fetch, validation, storage ----------
def test_fetch_stores_confirmed_candles_and_drops_the_open_one(db, kraken):
    kraken.replies["OHLC"] = ohlc(candles(40))
    result = cryptodata.fetch_kraken("btc-usd", 60, db, NOW)
    assert result["status"] == "OK" and result["requests"] == 2
    assert (result["received"], result["unconfirmed"], result["inserted"], result["rejected"]) == (41, 1, 40, 0)
    assert "pair=XBTUSD&interval=60" in kraken.calls[-1]
    assert (result["covered_from"], result["covered_to"]) == (LAST - 39 * H, LAST)
    row = db.execute("SELECT * FROM crypto_candles WHERE pair='BTC/USD' ORDER BY open_time DESC LIMIT 1").fetchone()
    assert (row["exchange"], row["market_type"], row["exchange_pair"], row["base"], row["quote"]) == ("KRAKEN", "SPOT", "XXBTZUSD", "BTC", "USD")
    assert (row["open_time"], row["confirmed"], row["trades"], row["origin"]) == (LAST, 1, 44, "API")


def test_abnormal_candles_are_rejected_and_recorded(db, kraken):
    rows = candles(30)                                                   # abnormal ones sit inside the stored range
    rows[10] = [rows[10][0], "10", "5", "8", "9", "9", "1", 1]           # high below low
    rows[11] = rows[11][:6] + ["-3", 1]                                  # negative volume
    rows[12] = [rows[12][0] + 60] + rows[12][1:]                         # not on the 60-minute grid
    rows[13] = rows[13][:4] + ["abc"] + rows[13][5:]                     # close not a number
    rows[14] = rows[14][:7] + [-2]                                       # negative trade count
    rows[15] = rows[15][:4] + ["nan"] + rows[15][5:]                     # not finite
    kraken.replies["OHLC"] = ohlc(rows)
    result = cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    assert result["status"] == "PARTIAL" and result["rejected"] == 6 and result["inserted"] == 24
    reasons = [r[0] for r in db.execute("SELECT reason FROM crypto_rejected")]
    for text in ("OHLC out of order", "negative volume", "grid", "not a number", "trade count", "finite"):
        assert any(text in r for r in reasons), text
    assert "rejected as abnormal" in " ".join(cryptodata.detail("BTC/USD", 60, NOW)["warnings"])


def test_duplicates_incremental_updates_and_conflicts(db, kraken):
    kraken.replies["OHLC"] = ohlc(candles(40))
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    assert cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)["status"] == "SKIPPED" and kraken.count("OHLC") == 1
    later = NOW + dt.timedelta(hours=2)                                   # two more candles have closed
    rows = candles(42, end=LAST + 2 * H)
    rows[10] = rows[10][:4] + [str(float(rows[10][4]) + 0.5)] + rows[10][5:]   # provider "revised" an old candle
    kraken.replies["OHLC"] = ohlc(rows)
    result = cryptodata.fetch_kraken("BTC/USD", 60, db, later)
    assert (result["inserted"], result["unchanged"], result["conflicts"], result["requests"]) == (2, 39, 1, 1)
    kept = db.execute("SELECT close FROM crypto_candles WHERE open_time=?", (rows[10][0],)).fetchone()[0]
    assert kept == float(candles(42, end=LAST + 2 * H)[10][4])           # original value kept, never overwritten
    assert count(db) == 42


@pytest.mark.parametrize("reply, kind", [
    ({"error": ["EAPI:Rate limit exceeded"]}, "rate_limit"),
    ({"error": ["EQuery:Unknown asset pair"]}, "unknown_pair"),
    ({"error": ["EService:Unavailable"]}, "service_unavailable"),
    ({"error": ["EGeneral:Invalid arguments"]}, "invalid_request"),
    (urllib.error.HTTPError("https://api.kraken.com/0/public/OHLC", 520, "x", {}, io.BytesIO(b"")), "http"),
    (urllib.error.URLError(ConnectionResetError()), "network"),
    (TimeoutError(), "timeout"),
    (b"<html>cloudflare</html>", "bad_response"),
    ({"error": [], "result": {"SOMETHINGELSE": [], "last": 0}}, "bad_response"),
])
def test_kraken_errors_store_nothing_and_never_retry(db, kraken, reply, kind):
    kraken.replies["OHLC"] = reply
    result = cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    assert result["status"] == "ERROR" and result["message"].startswith(kind)
    assert kraken.count("OHLC") == 1 and count(db) == 0


def test_gaps_are_classified_never_filled_and_delay_is_detected(db, kraken):
    first = candles(10, end=LAST - 30 * H)
    del first[4]                                                          # Kraken sent no candle here
    kraken.replies["OHLC"] = ohlc(first)
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    second = candles(10, end=LAST)                                        # a later window; 20 h in between never fetched
    second[3] = [second[3][0], "10", "5", "8", "9", "9", "1", 1]          # abnormal -> rejected
    kraken.replies["OHLC"] = ohlc(second)
    assert cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)["inserted"] == 9
    d = cryptodata.detail("BTC/USD", 60, NOW + dt.timedelta(hours=5))
    assert d["gaps"] == {"rejected": 1, "no_candle_from_exchange": 1, "not_collected": 20}
    assert count(db) == 18                                                # nothing was filled in
    text = " ".join(d["warnings"])
    assert "likely no trades" in text and "never collected" in text and "DELAYED" in text and d["behind_candles"] == 5


def test_database_survives_restart(db, kraken):
    kraken.replies["OHLC"] = ohlc(candles(30))
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    db.close()
    with contextlib.closing(cryptodata.connect()) as reopened:
        assert count(reopened) == 30 and reopened.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert reopened.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0   # stock table untouched, still separate


def test_csv_import_kraken_ohlcvt_and_header_formats(db, tmp_path):
    raw = tmp_path / "XBTUSD_60.csv"                                      # Kraken OHLCVT download: no header
    raw.write_text("\n".join(",".join(map(str, [c[0], *c[1:5], c[6], c[7]])) for c in candles(30)) + "\n", encoding="utf-8")
    result = cryptodata.import_csv(raw, db, "BTC/USD", 60, NOW)
    assert result["inserted"] == 30 and result["requests"] == 0
    headed = tmp_path / "eth.csv"
    headed.write_text("timestamp,open,high,low,close,volume\n" + "\n".join(",".join(map(str, [c[0], *c[1:5], c[6]])) for c in candles(30)),
                      encoding="utf-8")
    assert cryptodata.import_csv(headed, db, "ETH/USD", 60, NOW)["inserted"] == 30
    d = cryptodata.detail("BTC/USD", 60, NOW)
    assert d["status"] == "CSV · UNVERIFIED" and any("CSV" in w for w in d["warnings"])


def test_split_ohlcvt_archive_import_is_idempotent_and_gaps_are_labeled(db, kraken, tmp_path):
    import zipfile
    cryptodata.pair_map(db, NOW)                                          # altnames from (faked) AssetPairs
    rows = candles(48, end=LAST - 100 * H)
    del rows[20]                                                          # Kraken omits a bar with no trades
    text = "\n".join(",".join(map(str, [c[0], *c[1:5], c[6], c[7]])) for c in rows) + "\n"
    whole = tmp_path / "Kraken_OHLCVT_Full.zip"
    with zipfile.ZipFile(whole, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("XBTUSD_60.csv", text)
        zf.writestr("XBTUSDC_60.csv", "1,1,1,1,1,1,1\n")                 # similar name: must be ignored
        zf.writestr("MANIFEST.json", "{}")
    data, parts = whole.read_bytes(), tmp_path / "parts"
    parts.mkdir()
    for i in range(0, len(data), 700):                                    # split byte-by-byte like the real download
        (parts / f"Kraken_OHLCVT_Full.zip.part{i // 700:02d}").write_bytes(data[i:i + 700])
    first = cryptodata.import_archive(parts, db, ["BTC/USD"], [60, 5], batch=10, now_utc=NOW)
    assert [(r["interval"], r["status"]) for r in first] == [(60, "OK"), (5, "MISSING")]
    assert first[0]["inserted"] == 47 and first[0]["out_of_order"] == 0
    again = cryptodata.import_archive(parts, db, ["BTC/USD"], [60], batch=10, now_utc=NOW)[0]
    assert again["inserted"] == 0 and again["skipped_already_imported"] == 47 and count(db) == 47  # re-run adds nothing
    cutoff = sorted(r[0] for r in rows)[19]                               # simulate a crash after the first 2 batches
    with db:
        db.execute("DELETE FROM crypto_candles WHERE open_time > ?", (cutoff,))
    resumed = cryptodata.import_archive(parts, db, ["BTC/USD"], [60], batch=10, now_utc=NOW)[0]
    assert (resumed["skipped_already_imported"], resumed["inserted"]) == (20, 27) and count(db) == 47
    assert db.execute("SELECT COUNT(*) FROM crypto_candles WHERE open_time % (interval_min * 60) != 0").fetchone()[0] == 0
    kraken.replies["OHLC"] = ohlc(candles(30))                            # recent REST window, far after the file
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    kinds = [(g["kind"], g["missing"]) for g in cryptodata.gap_report(db, "BTC/USD", 60)]
    assert kinds == [("no_candle_from_exchange", 1), ("not_collected", 70)]  # LAST-99h .. LAST-30h


# ---------- indicators + Market Analyst ----------
def test_indicator_formulas_and_minimum_data():
    values = [float(i) for i in range(1, 41)]
    assert indicators.ema(values[:12], 12) == 6.5                         # seeded with the 12-value average
    k = 2 / 13
    assert indicators.ema(values[:13], 12) == round(13 * k + 6.5 * (1 - k), 4)
    assert indicators.ema(values[:11], 12) == indicators.INSUFFICIENT
    assert indicators.ema(values[:25], 26) == indicators.INSUFFICIENT and isinstance(indicators.ema(values[:26], 26), float)
    m = indicators.macd(values)
    assert m["macd"] == round(indicators.ema(values, 12) - indicators.ema(values, 26), 4) or abs(
        m["macd"] - (indicators.ema(values, 12) - indicators.ema(values, 26))) < 1e-3
    bars = [{"ts": str(i), "close": v, "volume": 1.0} for i, v in enumerate(values[:25])]
    a = market_analyst.analyze_bars("BTC/USD", bars, "crypto")
    assert a["status"] == "OK" and a["ema_26"] == indicators.INSUFFICIENT and a["macd"] == indicators.INSUFFICIENT


def test_market_report_for_crypto(db, kraken):
    kraken.replies["OHLC"] = ohlc(candles(40))
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    a = cryptodata.detail("BTC/USD", 60, NOW)["analysis"]
    assert (a["market"], a["market_type"], a["exchange"], a["pair"], a["interval"]) == ("crypto", "SPOT", "KRAKEN", "BTC/USD", "60m")
    assert a["analysis_time"] == "2026-11-16T00:00Z" and a["last_close"] == 139.0 and a["delayed"] is False
    assert a["status"] == "OK" and isinstance(a["ema_12"], float) and isinstance(a["macd"], dict) and a["rsi_14"] == 100.0
    assert "Kraken spot" in a["limitations"] and a["data_source"] == "HISTORICAL · KRAKEN SPOT"
    assert "broker" not in inspect.getsource(market_analyst) and "order" not in json.dumps(a).lower()


def test_grok_research_interface_takes_crypto_reports(xai):  # noqa: F811
    bars = [{"ts": cryptodata.iso(c[0]), "close": float(c[4]), "volume": float(c[6])} for c in candles(40)]
    report = market_analyst.market_report("BTC/USD", bars, "crypto", {"exchange": "KRAKEN", "interval": "60m"})
    xai.reply = chat({"analyses": [{"symbol": "BTC/USD", "view": "bullish", "confidence": 0.6, "rationale": "EMA12 above EMA26."}],
                      "summary": "MOCK answer"})
    research = market_analyst.research_real([report], "Kraken spot 60m confirmed candles up to 2026-11-16T00:00Z (HISTORICAL)")
    sent = json.loads(xai.calls[0]["body"]["messages"][1]["content"])
    assert sent["symbols"][0]["exchange"] == "KRAKEN" and sent["symbols"][0]["interval"] == "60m"
    assert research["summary"] == "MOCK answer" and len(xai.calls) == 1   # only because this test asked, with a fake key


# ---------- dashboard API + safety ----------
def test_crypto_dashboard_api(db, kraken):
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        overview = client.get("/api/crypto").json()
        assert len(overview["pairs"]) == 18 and all(p["status"] == "DATA UNAVAILABLE" for p in overview["pairs"])
        kraken.replies["OHLC"] = ohlc(candles(40))
        cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
        d = client.get("/api/crypto/btc-usd?interval=60").json()
        assert d["rows"] == 40 and len(d["candles"]) == len(d["sma20"]) == len(d["ema12"]) == 40
        assert d["status"] == "HISTORICAL · KRAKEN SPOT" and d["grok"].startswith("GROK OFFLINE") and "not real-time" in d["note"]
        assert client.get("/api/crypto/ETH-USD?interval=5").json()["status"] == "DATA UNAVAILABLE"
        assert client.get("/api/crypto/DOGE-USD").status_code == 404
        assert client.get("/api/crypto/BTC-USD?interval=30").status_code == 400   # 30 min is not one of the 6
        assert client.get("/api/stocks/SPY").status_code == 200              # stocks still served


def test_real_crypto_data_never_reaches_the_trading_simulation(db, kraken):
    kraken.replies["OHLC"] = ohlc(candles(40, start=5.0))                  # absurd BTC prices, easy to spot
    cryptodata.fetch_kraken("BTC/USD", 60, db, NOW)
    floor = TradingFloor(seed=4)
    floor.broker.reset()
    for _ in range(3):
        floor.run_cycle()
    assert floor.market.prices["BTC-USD"] > 1000 and "cryptodata" not in inspect.getsource(engine)
    assert all(t["price"] > 1000 for t in floor.broker.trades if t["symbol"] == "BTC-USD")
