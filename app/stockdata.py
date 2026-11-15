"""Real US stock daily bars: Alpha Vantage (TIME_SERIES_DAILY, compact) or a CSV you provide, stored in SQLite.

DISPLAY / RESEARCH ONLY. This data never feeds the trading simulation, its orders or balances.
Nothing here runs by itself; use the commands:

    python -m app.stockdata status
    python -m app.stockdata fetch SPY            (needs ALPHAVANTAGE_API_KEY; one request per symbol)
    python -m app.stockdata import-csv FILE.csv --symbol SPY [--provider NAME] [--data-type RAW|ADJUSTED]

Update policy: a stored bar is never overwritten. Re-sending identical values counts as "unchanged";
different values for an existing (symbol, date, data type) are kept out and reported as "conflicts".
"""

import argparse
import contextlib
import csv
import datetime as dt
import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, indicators
from .agents import market_analyst
from .us_calendar import bar_is_final, is_trading_day, last_completed_session, trading_days

log = logging.getLogger("stocks")

SYMBOLS = ("SPY", "QQQ", "AAPL", "NVDA", "TSLA")
DB_PATH = config.DATA_DIR / "market_data.db"
AV_URL = "https://www.alphavantage.co/query"
AV_PROVIDER = "ALPHA_VANTAGE"
AV_SOURCE = "TIME_SERIES_DAILY outputsize=compact"
AV_DAILY_LIMIT = 25        # free tier: 25 requests per day (alphavantage.co/premium)
AV_GAP_SECONDS = 12        # pause between symbols so several fetches never burst
TIMEOUT_SECONDS = 30
MAX_CSV_ROWS = 50_000
SPLIT_GAP = 0.40           # a >40% jump between sessions is flagged as a possible split / bad data

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    asset_class  TEXT NOT NULL CHECK (asset_class IN ('stock', 'crypto')),
    symbol       TEXT NOT NULL,
    interval     TEXT NOT NULL CHECK (interval IN ('daily')),
    ts           TEXT NOT NULL,              -- trading date YYYY-MM-DD (US/Eastern session)
    data_type    TEXT NOT NULL CHECK (data_type IN ('RAW', 'ADJUSTED')),
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume       INTEGER NOT NULL CHECK (volume >= 0),
    provider     TEXT NOT NULL,              -- ALPHA_VANTAGE, the provider a CSV names, or UNKNOWN
    origin       TEXT NOT NULL CHECK (origin IN ('API', 'CSV')),
    source       TEXT NOT NULL,              -- API request or CSV file name
    collected_at TEXT NOT NULL,              -- UTC time this row was stored
    CHECK (low > 0 AND low <= open AND low <= close AND high >= open AND high >= close),
    PRIMARY KEY (asset_class, symbol, interval, ts, data_type)
);
CREATE TABLE IF NOT EXISTS ingest_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    time         TEXT NOT NULL,
    asset_class  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    provider     TEXT NOT NULL,
    origin       TEXT NOT NULL,
    source       TEXT NOT NULL,
    request_sent INTEGER NOT NULL,           -- 1 if an HTTP request went out (counts toward the daily limit)
    status       TEXT NOT NULL,              -- OK, PARTIAL, ERROR, SKIPPED
    received INTEGER, inserted INTEGER, unchanged INTEGER, conflicts INTEGER, rejected INTEGER, incomplete INTEGER,
    message      TEXT
);
"""
FIELDS = ("open", "high", "low", "close", "volume")


class ProviderError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def redact(text):
    """Remove the Alpha Vantage key from anything we log, store or show (its rate-limit notes can echo it)."""
    text = str(text)
    return text.replace(config.ALPHAVANTAGE_API_KEY, "[redacted]") if config.ALPHAVANTAGE_API_KEY else text


def connect(path=None):
    """Open (and create if needed) the market data DB. WAL keeps readers working while a fetch writes."""
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------- validation (shared with app/cryptodata.py) ----------
def number(raw, field):
    """(float, None) or (None, reason) for one provider / CSV value."""
    value = str(raw.get(field, "") or "").strip().replace(",", "")
    if not value:
        return None, f"missing {field}"
    try:
        num = float(value)
    except ValueError:
        return None, f"{field} is not a number"
    return (num, None) if math.isfinite(num) else (None, f"{field} is not a finite number")


def ohlc_problem(row):
    """Why open/high/low/close can't be a real candle, or None. Also rejects negative volume."""
    if row["volume"] < 0:
        return "negative volume"
    if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
        return "price must be above 0"
    if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
        return "OHLC out of order (high/low don't contain open/close)"
    return None


def _parse(raw, today):
    """One raw row -> (clean row, None) or (None, reason it was rejected)."""
    try:
        day = dt.date.fromisoformat(str(raw.get("ts", "")).strip())
    except ValueError:
        return None, "missing or bad date"
    row = {"ts": day.isoformat()}
    for f in FIELDS:
        row[f], problem = number(raw, f)
        if problem:
            return None, problem
    if ohlc_problem(row):
        return None, ohlc_problem(row)
    if row["volume"] != int(row["volume"]):
        return None, "volume is not a whole number"
    row["volume"] = int(row["volume"])
    if day > today:
        return None, "date is in the future"
    if day.weekday() >= 5:
        return None, "weekend date (US market closed)"
    return row, None


def validate(raw_rows, now_utc):
    """Split raw rows into (good rows sorted by date, [(date, reason)], count of unfinished sessions)."""
    good, rejected, incomplete, seen = [], [], 0, set()
    for raw in raw_rows:
        row, problem = _parse(raw, now_utc.date())
        if problem:
            rejected.append((str(raw.get("ts", "?")), problem))
        elif row["ts"] in seen:
            rejected.append((row["ts"], "same date twice in one batch (kept the first)"))
        elif not bar_is_final(dt.date.fromisoformat(row["ts"]), now_utc):
            incomplete += 1  # today's session hasn't closed: the bar can still change, so don't store it
        else:
            seen.add(row["ts"])
            good.append(row)
    return sorted(good, key=lambda r: r["ts"]), rejected, incomplete


# ---------- storage ----------
def _store(conn, symbol, rows, data_type, provider, origin, source, now_utc):
    """Insert new bars; never overwrite. Returns (inserted, unchanged, conflict dates). Caller owns the transaction."""
    inserted, unchanged, conflicts = 0, 0, []
    for r in rows:
        old = conn.execute("SELECT open, high, low, close, volume FROM bars WHERE asset_class='stock' AND symbol=? "
                           "AND interval='daily' AND ts=? AND data_type=?", (symbol, r["ts"], data_type)).fetchone()
        if old is None:
            conn.execute("INSERT INTO bars VALUES ('stock', ?, 'daily', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (symbol, r["ts"], data_type, r["open"], r["high"], r["low"], r["close"], r["volume"],
                          provider, origin, source, now_utc.isoformat(timespec="seconds")))
            inserted += 1
        elif all(round(old[f], 4) == round(r[f], 4) for f in FIELDS):
            unchanged += 1
        else:
            conflicts.append(r["ts"])
    return inserted, unchanged, conflicts


def _log(conn, symbol, provider, origin, source, sent, status, now_utc, message="", **counts):
    with conn:
        conn.execute("INSERT INTO ingest_log (time, asset_class, symbol, provider, origin, source, request_sent, status, "
                     "received, inserted, unchanged, conflicts, rejected, incomplete, message) "
                     "VALUES (?, 'stock', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (now_utc.isoformat(timespec="seconds"), symbol, provider, origin, source, int(sent), status,
                      counts.get("received"), counts.get("inserted"), counts.get("unchanged"), counts.get("conflicts"),
                      counts.get("rejected"), counts.get("incomplete"), redact(message)[:500]))
    return {"symbol": symbol, "status": status, "request_sent": bool(sent), "message": redact(message)[:500], **counts}


def _ingest(conn, symbol, raw_rows, data_type, provider, origin, source, sent, now_utc, note=""):
    """Validate + store one batch atomically, then log it. A failure stores nothing from the batch."""
    good, rejected, incomplete = validate(raw_rows, now_utc)
    try:
        with conn:  # one transaction: commit all or roll back all
            inserted, unchanged, conflicts = _store(conn, symbol, good, data_type, provider, origin, source, now_utc)
    except sqlite3.Error as err:
        return _log(conn, symbol, provider, origin, source, sent, "ERROR", now_utc,
                    f"database error ({type(err).__name__}); nothing from this batch was stored", received=len(raw_rows))
    parts = [note] if note else []
    if rejected:
        parts.append(f"rejected {len(rejected)}: " + "; ".join(f"{d} {why}" for d, why in rejected[:10]))
    if conflicts:
        parts.append(f"kept existing values for {len(conflicts)} date(s) with different data: " + ", ".join(conflicts[:10]))
    if incomplete:
        parts.append(f"skipped {incomplete} unfinished session(s)")
    status = "OK" if not rejected and not conflicts else "PARTIAL"
    return _log(conn, symbol, provider, origin, source, sent, status, now_utc, " | ".join(parts),
                received=len(raw_rows), inserted=inserted, unchanged=unchanged, conflicts=len(conflicts),
                rejected=len(rejected), incomplete=incomplete)


# ---------- Alpha Vantage ----------
def parse_alpha_vantage(payload, symbol):
    """Raw rows from a TIME_SERIES_DAILY JSON answer, or ProviderError. Alpha Vantage reports errors with HTTP 200."""
    if not isinstance(payload, dict):
        raise ProviderError("bad_response", "Answer is not a JSON object.")
    if "Error Message" in payload:
        msg = str(payload["Error Message"])
        raise ProviderError("auth" if "apikey" in msg.lower() else "invalid_request", msg)
    for key in ("Note", "Information"):
        if key in payload:
            msg, low = str(payload[key]), str(payload[key]).lower()
            if key == "Note" or any(w in low for w in ("rate limit", "requests per day", "call frequency", "sparingly")):
                raise ProviderError("rate_limit", msg)
            raise ProviderError("premium" if "premium" in low else "provider_info", msg)
    meta, series = payload.get("Meta Data"), payload.get("Time Series (Daily)")
    if not isinstance(series, dict) or not series:
        raise ProviderError("bad_response", "No 'Time Series (Daily)' in the answer.")
    if isinstance(meta, dict) and str(meta.get("2. Symbol", symbol)).upper() != symbol:
        raise ProviderError("bad_response", f"Answer is for {meta.get('2. Symbol')}, not {symbol}.")
    keys = {"open": "1. open", "high": "2. high", "low": "3. low", "close": "4. close", "volume": "5. volume"}
    return [{"ts": day, **{f: v.get(k) for f, k in keys.items()}} for day, v in series.items() if isinstance(v, dict)]


def requests_today(conn, now_utc):
    """Alpha Vantage requests sent today (UTC day; their reset time isn't documented)."""
    return conn.execute("SELECT COUNT(*) FROM ingest_log WHERE provider=? AND request_sent=1 AND substr(time, 1, 10)=?",
                        (AV_PROVIDER, now_utc.date().isoformat())).fetchone()[0]


def fetch_alpha_vantage(symbol, conn, now_utc=None):
    """At most ONE TIME_SERIES_DAILY (compact) request for one stock; validates and stores. Never retries."""
    now, symbol = now_utc or _utcnow(), symbol.upper()

    def skip(message, status="SKIPPED"):
        return _log(conn, symbol, AV_PROVIDER, "API", AV_SOURCE, False, status, now, message)

    if symbol not in SYMBOLS:
        return {"symbol": symbol, "status": "ERROR", "request_sent": False, "message": f"Only {', '.join(SYMBOLS)} are supported."}
    if not config.ALPHAVANTAGE_API_KEY:
        return skip("DATA UNAVAILABLE: no ALPHAVANTAGE_API_KEY in .env. Import a CSV instead.")
    used = requests_today(conn, now)
    if used >= AV_DAILY_LIMIT:
        return skip(f"Free daily limit reached ({used}/{AV_DAILY_LIMIT} requests today). Not sending.")
    latest = latest_date(conn, symbol, "RAW")
    if latest and latest >= last_completed_session(now).isoformat():
        return skip(f"Already up to date (latest bar {latest}). Not sending.")

    url = AV_URL + "?" + urllib.parse.urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact",
                                                 "datatype": "json", "apikey": config.ALPHAVANTAGE_API_KEY})
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:  # never log `url`: it holds the key
            rows = parse_alpha_vantage(json.loads(resp.read().decode("utf-8")), symbol)
    except ProviderError as err:
        kind, message = err.kind, str(err)
    except urllib.error.HTTPError as err:
        kind, message = ("rate_limit" if err.code == 429 else "auth" if err.code in (401, 403) else "http"), f"HTTP {err.code}"
    except TimeoutError:
        kind, message = "timeout", f"No answer within {TIMEOUT_SECONDS}s."
    except urllib.error.URLError as err:
        kind = "timeout" if isinstance(err.reason, TimeoutError) else "network"
        message = f"Network error ({type(err.reason).__name__})."
    except ValueError:
        kind, message = "bad_response", "Answer is not valid JSON."
    else:
        return _ingest(conn, symbol, rows, "RAW", AV_PROVIDER, "API", AV_SOURCE, True, now)
    log.warning("Alpha Vantage %s for %s: %s", kind, symbol, redact(message)[:200])
    return _log(conn, symbol, AV_PROVIDER, "API", AV_SOURCE, True, "ERROR", now, f"{kind}: {message}")


# ---------- CSV ----------
def import_csv(path, conn, symbol=None, provider=None, data_type=None, now_utc=None):
    """Import daily bars from a CSV you obtained legally. Needs Date, Open, High, Low, Close, Volume columns;
    Symbol, Provider and DataType are optional. CSV rows are never treated as verified provider data."""
    now = now_utc or _utcnow()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower().replace(" ", "").replace("_", ""): c for c in (reader.fieldnames or [])}
        if "date" not in cols and "timestamp" in cols:
            cols["date"] = cols["timestamp"]
        missing = [c for c in ("date", "open", "high", "low", "close", "volume") if c not in cols]
        if missing:
            raise ValueError(f"CSV is missing column(s): {', '.join(missing)}")
        records = []
        for i, rec in enumerate(reader):
            if i >= MAX_CSV_ROWS:
                raise ValueError(f"CSV has more than {MAX_CSV_ROWS} rows.")
            records.append(rec)

    def col(rec, name):
        return (rec.get(cols[name]) or "").strip() if name in cols else ""

    symbols = {col(r, "symbol").upper() for r in records} - {""}
    symbol = (symbol or (symbols.pop() if len(symbols) == 1 else "")).upper()
    if symbol not in SYMBOLS:
        raise ValueError(f"Give --symbol (one of {', '.join(SYMBOLS)}); the file names {sorted(symbols) or 'no symbol'}.")
    other = {s for s in {col(r, 'symbol').upper() for r in records} if s and s != symbol}
    if other:
        raise ValueError(f"CSV mixes symbols {sorted(other)} with {symbol}; import one symbol per file.")
    provider = (provider or next((col(r, "provider") for r in records if col(r, "provider")), "") or "UNKNOWN").upper()[:60]
    stated = data_type or next((col(r, "datatype") for r in records if col(r, "datatype")), "")
    dtype = (stated or "RAW").upper()
    if dtype not in ("RAW", "ADJUSTED"):
        raise ValueError("Data type must be RAW or ADJUSTED.")
    note = "" if stated else "data type not stated in the file; stored as RAW (unverified)"
    raw_rows = [{"ts": col(r, "date"), **{f: col(r, f) for f in FIELDS}} for r in records]
    return _ingest(conn, symbol, raw_rows, dtype, provider, "CSV", f"csv:{Path(path).name}", False, now, note)


# ---------- reading ----------
def latest_date(conn, symbol, data_type):
    row = conn.execute("SELECT MAX(ts) FROM bars WHERE asset_class='stock' AND symbol=? AND interval='daily' AND data_type=?",
                       (symbol, data_type)).fetchone()
    return row[0]


def load_bars(conn, symbol):
    """Stored daily bars, oldest first, of ONE data type (RAW preferred) so a series never mixes RAW and ADJUSTED."""
    types = [r[0] for r in conn.execute("SELECT DISTINCT data_type FROM bars WHERE asset_class='stock' AND symbol=?", (symbol,))]
    dtype = "RAW" if "RAW" in types else (types[0] if types else None)
    rows = conn.execute("SELECT * FROM bars WHERE asset_class='stock' AND symbol=? AND interval='daily' AND data_type=? "
                        "ORDER BY ts", (symbol, dtype)).fetchall()
    return [dict(r) for r in rows], dtype, types


def quality(bars, types, now_utc):
    """Plain-language data problems for the dashboard. Empty list = nothing found."""
    if not bars:
        return ["DATA UNAVAILABLE: no stored bars for this symbol."]
    warnings, dates = [], [b["ts"] for b in bars]
    holiday = [d for d in dates if not is_trading_day(dt.date.fromisoformat(d))]
    if holiday:
        warnings.append(f"{len(holiday)} bar(s) on days the US market calendar marks as holidays: {', '.join(holiday[:5])}")
    missing = sorted(set(d.isoformat() for d in trading_days(dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1]))) - set(dates))
    if missing:
        warnings.append(f"{len(missing)} trading day(s) missing between {dates[0]} and {dates[-1]}: {', '.join(missing[:5])}"
                        + (" ..." if len(missing) > 5 else ""))
    for prev, cur in zip(bars, bars[1:]):
        gap = cur["open"] / prev["close"] - 1
        if abs(gap) > SPLIT_GAP:
            warnings.append(f"{cur['ts']}: price jumped {gap * 100:+.0f}% from the previous close. Possible stock split "
                            "or bad data; RAW prices are not split-adjusted, so indicators around this date are unreliable.")
    zero = [b["ts"] for b in bars if b["volume"] == 0]
    if zero:
        warnings.append(f"{len(zero)} bar(s) with zero volume: {', '.join(zero[:5])}")
    expected = last_completed_session(now_utc).isoformat()
    behind = len(trading_days(dt.date.fromisoformat(dates[-1]), dt.date.fromisoformat(expected))) - 1
    if behind > 0:
        warnings.append(f"Latest bar is {dates[-1]}; {behind} completed session(s) since then are not stored (last completed: {expected}).")
    if any(b["origin"] == "CSV" and b["provider"] == "UNKNOWN" for b in bars):
        warnings.append("Some bars came from a CSV with no named provider: source UNKNOWN, not verified.")
    if len(types) > 1:
        warnings.append(f"Stored data types {sorted(types)}; charts and analysis use RAW only, never a mix.")
    return warnings


def _status_label(bars):
    if not bars:
        return "DATA UNAVAILABLE"
    sources = {(b["origin"], b["provider"]) for b in bars}
    if sources == {("API", AV_PROVIDER)}:
        return "REAL DATA · ALPHA VANTAGE"
    if all(o == "CSV" for o, _ in sources):
        return "CSV · UNVERIFIED" + (" · SOURCE UNKNOWN" if ("CSV", "UNKNOWN") in sources else "")
    return "MIXED SOURCES · PARTLY UNVERIFIED"


def recent_log(conn, symbol, limit=5):
    return [dict(r) for r in conn.execute("SELECT * FROM ingest_log WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))]


def detail(symbol, now_utc=None):
    """Everything the Stocks screen shows for one symbol. Read-only."""
    now = now_utc or _utcnow()
    with contextlib.closing(connect()) as conn:
        bars, dtype, types = load_bars(conn, symbol)
        log_rows = recent_log(conn, symbol)
    closes = [b["close"] for b in bars]
    warnings = quality(bars, types, now)
    context = {"market_type": "STOCK", "exchange": ", ".join(sorted({b["provider"] for b in bars})) or None,
               "interval": "1d", "analysis_time": bars[-1]["ts"] if bars else None, "data_source": _status_label(bars),
               "data_quality": "OK" if not warnings else f"{len(warnings)} warning(s)",
               "delayed": bool(bars) and bars[-1]["ts"] < last_completed_session(now).isoformat()}
    return {
        "symbol": symbol, "asset_class": "stock", "interval": "daily", "data_type": dtype,
        "status": _status_label(bars), "rows": len(bars),
        "first": bars[0]["ts"] if bars else None, "last": bars[-1]["ts"] if bars else None,
        "providers": sorted({b["provider"] for b in bars}), "origins": sorted({b["origin"] for b in bars}),
        "last_collected": max((b["collected_at"] for b in bars), default=None),
        "last_completed_session": last_completed_session(now).isoformat(),
        "bars": bars[-120:],
        "sma5": indicators.sma_series(closes, 5)[-120:], "sma20": indicators.sma_series(closes, 20)[-120:],
        "analysis": market_analyst.market_report(symbol, bars, "stock", context),
        "warnings": warnings,
        "log": log_rows,
        "note": "Historical daily bars, not real-time. Display/research only: never used by the trading simulation.",
    }


def overview(now_utc=None):
    """One line per supported stock for the Stocks screen."""
    out = []
    for symbol in SYMBOLS:
        d = detail(symbol, now_utc)
        out.append({k: d[k] for k in ("symbol", "status", "rows", "last", "data_type")} | {"warnings": len(d["warnings"]) if d["rows"] else 0})
    return out


# ---------- command line ----------
def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-8s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(prog="python -m app.stockdata", description="Real US stock daily bars (research only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch", help="Alpha Vantage TIME_SERIES_DAILY (compact), one request per symbol")
    fetch.add_argument("symbols", nargs="*", default=list(SYMBOLS))
    imp = sub.add_parser("import-csv", help="import a CSV you obtained legally")
    imp.add_argument("file")
    imp.add_argument("--symbol")
    imp.add_argument("--provider")
    imp.add_argument("--data-type", choices=["RAW", "ADJUSTED"])
    sub.add_parser("status", help="what is stored")
    args = parser.parse_args(argv)

    with contextlib.closing(connect()) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("data/market_data.db failed its integrity check. Not writing to it.")
        if args.cmd == "fetch":
            for i, sym in enumerate(args.symbols):
                result = fetch_alpha_vantage(sym, conn)
                print(json.dumps(result))
                if result["request_sent"] and i < len(args.symbols) - 1:
                    time.sleep(AV_GAP_SECONDS)
        elif args.cmd == "import-csv":
            print(json.dumps(import_csv(Path(args.file), conn, args.symbol, args.provider, args.data_type)))
    if args.cmd == "status":
        print(json.dumps(overview(), indent=2))


if __name__ == "__main__":
    main()
