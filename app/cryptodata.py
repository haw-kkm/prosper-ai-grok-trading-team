"""Real crypto candles from Kraken spot (public REST API, no key): BTC/USD, ETH/USD, SOL/USD, 60-min and 5-min.

DISPLAY / RESEARCH ONLY: never feeds the trading simulation, its orders or balances. Nothing runs by itself:

    python -m app.cryptodata status
    python -m app.cryptodata fetch BTC/USD --interval 60   (1 OHLC request, +1 AssetPairs request if the pair map is stale)
    python -m app.cryptodata import-csv FILE.csv --pair BTC/USD --interval 60   (Kraken OHLCVT download or your own CSV)

Kraken returns at most the 720 most recent candles per interval (`since` cannot reach older data) and its last
candle is the still-open one, which is never stored. Stored candles are never overwritten (same policy as stocks).
Crypto lives in its own tables, keyed by exchange + market type (SPOT/FUTURES), so Kraken spot never mixes with
stocks or with futures data from another exchange.
"""

import argparse
import bisect
import contextlib
import csv
import datetime as dt
import io
import itertools
import json
import logging
import math
import os
import sqlite3
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import grok, indicators
from .agents import market_analyst
from .stockdata import ProviderError, _utcnow, number, ohlc_problem
from .stockdata import connect as _connect

log = logging.getLogger("crypto")

EXCHANGE, MARKET = "KRAKEN", "SPOT"
PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD")
INTERVALS = (60, 1, 5, 15, 240, 1440)  # minutes; 60 is the default
API = "https://api.kraken.com/0/public/"
TIMEOUT_SECONDS = 30
GAP_SECONDS = 2                     # pause between requests when the CLI fetches several
PAIR_MAP_MAX_AGE = dt.timedelta(days=7)
ASSET_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
MAX_CSV_ROWS = 2_000_000
CHART_CANDLES = 300
FIELDS = ("open", "high", "low", "close", "volume")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_candles (
    exchange      TEXT NOT NULL,
    market_type   TEXT NOT NULL CHECK (market_type IN ('SPOT', 'FUTURES')),
    pair          TEXT NOT NULL,             -- display name, e.g. BTC/USD
    exchange_pair TEXT NOT NULL,             -- the exchange's own key, e.g. XXBTZUSD
    base TEXT NOT NULL, quote TEXT NOT NULL,
    interval_min  INTEGER NOT NULL CHECK (interval_min > 0),
    open_time     INTEGER NOT NULL,          -- candle start, Unix seconds (UTC)
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, vwap REAL,
    volume        REAL NOT NULL CHECK (volume >= 0),
    trades        INTEGER CHECK (trades IS NULL OR trades >= 0),
    confirmed     INTEGER NOT NULL CHECK (confirmed = 1),   -- only closed candles are ever stored
    origin        TEXT NOT NULL CHECK (origin IN ('API', 'CSV')),
    source        TEXT NOT NULL,
    collected_at  TEXT NOT NULL,             -- UTC time this row was stored
    CHECK (low > 0 AND low <= open AND low <= close AND high >= open AND high >= close),
    PRIMARY KEY (exchange, market_type, pair, interval_min, open_time)
);
CREATE TABLE IF NOT EXISTS crypto_fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, exchange TEXT NOT NULL, market_type TEXT NOT NULL,
    pair TEXT NOT NULL, interval_min INTEGER NOT NULL, origin TEXT NOT NULL, source TEXT NOT NULL,
    requests INTEGER NOT NULL,               -- HTTP requests sent to the exchange for this attempt
    status TEXT NOT NULL,                    -- OK, PARTIAL, ERROR, SKIPPED
    received INTEGER, unconfirmed INTEGER, inserted INTEGER, unchanged INTEGER, conflicts INTEGER, rejected INTEGER,
    covered_from INTEGER, covered_to INTEGER, -- open times this answer covered: a gap inside = the exchange sent no candle
    message TEXT
);
CREATE TABLE IF NOT EXISTS crypto_rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, exchange TEXT NOT NULL, market_type TEXT NOT NULL,
    pair TEXT NOT NULL, interval_min INTEGER NOT NULL, open_time INTEGER, reason TEXT NOT NULL, raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crypto_pairs (
    exchange TEXT NOT NULL, market_type TEXT NOT NULL, pair TEXT NOT NULL, exchange_pair TEXT NOT NULL,
    altname TEXT NOT NULL, wsname TEXT, base TEXT NOT NULL, quote TEXT NOT NULL, status TEXT, verified_at TEXT NOT NULL,
    PRIMARY KEY (exchange, market_type, pair)
);
"""


def connect(path=None):
    """The same data/market_data.db as stocks (stock tables untouched), plus the crypto tables."""
    conn = _connect(path)
    conn.executescript(SCHEMA)
    return conn


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")


def last_closed_open(now_utc, interval):
    """Open time of the most recent candle that has fully closed."""
    step = interval * 60
    return int(now_utc.timestamp()) // step * step - step


# ---------- Kraken requests ----------
def _get(endpoint, params):
    """ONE public GET to Kraken. Returns the JSON answer or raises ProviderError. Never retries."""
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise ProviderError("rate_limit" if err.code == 429 else "http", f"HTTP {err.code}") from None
    except TimeoutError:
        raise ProviderError("timeout", f"No answer within {TIMEOUT_SECONDS}s.") from None
    except urllib.error.URLError as err:
        kind = "timeout" if isinstance(err.reason, TimeoutError) else "network"
        raise ProviderError(kind, f"Network error ({type(err.reason).__name__}).") from None
    except ValueError:
        raise ProviderError("bad_response", "Answer is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise ProviderError("bad_response", "Answer is not a JSON object.")
    errors = payload.get("error") or []
    if errors:
        msg = "; ".join(map(str, errors))[:300]
        kind = ("rate_limit" if "Rate limit" in msg else "unknown_pair" if "Unknown asset pair" in msg
                else "service_unavailable" if "EService" in msg else "invalid_request" if "EGeneral" in msg else "provider_error")
        raise ProviderError(kind, msg)
    if not isinstance(payload.get("result"), dict):
        raise ProviderError("bad_response", "Answer has no 'result' object.")
    return payload


def _display(asset):
    """Kraken asset code -> display code: XXBT -> XBT -> BTC, ZUSD -> USD, SOL -> SOL."""
    code = asset[1:] if len(asset) == 4 and asset[0] in "XZ" else asset
    return ASSET_ALIASES.get(code, code)


def pair_map(conn, now_utc):
    """Display pair -> Kraken names, as listed by Kraken's AssetPairs (cached 7 days).
    Returns (map, requests sent). A pair Kraken doesn't list is simply absent, never invented."""
    rows = {r["pair"]: dict(r) for r in conn.execute("SELECT * FROM crypto_pairs WHERE exchange=? AND market_type=?",
                                                       (EXCHANGE, MARKET))}
    if all(p in rows and now_utc - dt.datetime.fromisoformat(rows[p]["verified_at"]) < PAIR_MAP_MAX_AGE for p in PAIRS):
        return rows, 0
    payload = _get("AssetPairs", {"pair": ",".join(PAIRS)})
    found = {}
    for key, info in payload["result"].items():
        if not isinstance(info, dict):
            continue
        ws = str(info.get("wsname") or "")
        name = "/".join(_display(a) for a in ws.split("/")) if "/" in ws else \
            f"{_display(str(info.get('base', '')))}/{_display(str(info.get('quote', '')))}"
        if name in PAIRS:
            found[name] = {"exchange": EXCHANGE, "market_type": MARKET, "pair": name, "exchange_pair": key,
                           "altname": str(info.get("altname") or key), "wsname": ws, "base": name.split("/")[0],
                           "quote": name.split("/")[1], "status": info.get("status"),
                           "verified_at": now_utc.isoformat(timespec="seconds")}
    with conn:
        for r in found.values():
            conn.execute("INSERT OR REPLACE INTO crypto_pairs VALUES (:exchange, :market_type, :pair, :exchange_pair, "
                         ":altname, :wsname, :base, :quote, :status, :verified_at)", r)
    return found, 1


# ---------- validation + storage ----------
def _parse(raw, step):
    """One raw candle -> (clean row, None) or (None, reason it was rejected)."""
    try:
        t = float(str(raw.get("time", "")).strip())
    except ValueError:
        return None, "missing or bad time"
    if not math.isfinite(t) or t != int(t) or t <= 0:
        return None, "missing or bad time"
    row = {"open_time": int(t)}
    if row["open_time"] % step:
        return None, f"time not on the {step // 60}-minute grid"
    for f in FIELDS:
        row[f], problem = number(raw, f)
        if problem:
            return None, problem
    problem = ohlc_problem(row)
    if problem:
        return None, problem
    vwap, bad = number(raw, "vwap")
    row["vwap"] = vwap if not bad and vwap > 0 else None
    row["trades"] = None
    if str(raw.get("trades", "") or "").strip():
        trades, bad = number(raw, "trades")
        if bad or trades < 0 or trades != int(trades):
            return None, "trade count is not a whole number >= 0"
        row["trades"] = int(trades)
    return row, None


def _log(conn, pair, interval, origin, source, requests, status, now_utc, message="", **counts):
    with conn:
        conn.execute("INSERT INTO crypto_fetch_log (time, exchange, market_type, pair, interval_min, origin, source, requests, "
                     "status, received, unconfirmed, inserted, unchanged, conflicts, rejected, covered_from, covered_to, message) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (now_utc.isoformat(timespec="seconds"), EXCHANGE, MARKET, pair, interval, origin, source, requests, status,
                      counts.get("received"), counts.get("unconfirmed"), counts.get("inserted"), counts.get("unchanged"),
                      counts.get("conflicts"), counts.get("rejected"), counts.get("covered_from"), counts.get("covered_to"),
                      message[:600]))
    return {"pair": pair, "interval": interval, "status": status, "requests": requests, "message": message[:600], **counts}


def _ingest(conn, info, interval, raw_rows, origin, source, requests, now_utc, received, unconfirmed=0):
    """Validate + store one batch in ONE transaction (all or nothing), record rejects, then log it."""
    step, now_ts = interval * 60, now_utc.timestamp()
    good, rejected, seen = [], [], set()
    for raw in raw_rows:
        row, problem = _parse(raw, step)
        if problem:
            rejected.append((raw, problem))
        elif row["open_time"] + step > now_ts:
            unconfirmed += 1                     # candle hasn't closed yet: it can still change
        elif row["open_time"] in seen:
            rejected.append((raw, "same candle twice in one batch (kept the first)"))
        else:
            seen.add(row["open_time"])
            good.append(row)
    good.sort(key=lambda r: r["open_time"])
    key = (EXCHANGE, MARKET, info["pair"], interval)
    inserted, unchanged, conflicts = 0, 0, []
    try:
        with conn:
            for r in good:
                old = conn.execute("SELECT open, high, low, close, volume FROM crypto_candles WHERE exchange=? AND "
                                   "market_type=? AND pair=? AND interval_min=? AND open_time=?", (*key, r["open_time"])).fetchone()
                if old is None:
                    conn.execute("INSERT INTO crypto_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                                 (EXCHANGE, MARKET, info["pair"], info["exchange_pair"], info["base"], info["quote"], interval,
                                  r["open_time"], r["open"], r["high"], r["low"], r["close"], r["vwap"], r["volume"],
                                  r["trades"], origin, source, now_utc.isoformat(timespec="seconds")))
                    inserted += 1
                elif all(round(old[f], 8) == round(r[f], 8) for f in FIELDS):
                    unchanged += 1
                else:
                    conflicts.append(r["open_time"])
            for raw, why in rejected:
                t = raw.get("time")
                conn.execute("INSERT INTO crypto_rejected (time, exchange, market_type, pair, interval_min, open_time, reason, raw) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (now_utc.isoformat(timespec="seconds"), *key,
                                                                  int(t) if str(t).isdigit() else None, why, json.dumps(raw)[:500]))
    except sqlite3.Error as err:
        return _log(conn, info["pair"], interval, origin, source, requests, "ERROR", now_utc,
                    f"database error ({type(err).__name__}); nothing from this batch was stored", received=received)
    parts = []
    if rejected:
        parts.append(f"rejected {len(rejected)} abnormal candle(s): " + "; ".join(f"{r.get('time')} {why}" for r, why in rejected[:8]))
    if conflicts:
        parts.append(f"kept stored values for {len(conflicts)} candle(s) that came back different: " + ", ".join(iso(t) for t in conflicts[:8]))
    if unconfirmed:
        parts.append(f"left out {unconfirmed} unconfirmed (still open) candle(s)")
    return _log(conn, info["pair"], interval, origin, source, requests, "OK" if not rejected and not conflicts else "PARTIAL",
                now_utc, " | ".join(parts), received=received, unconfirmed=unconfirmed, inserted=inserted,
                unchanged=unchanged, conflicts=len(conflicts), rejected=len(rejected),
                covered_from=good[0]["open_time"] if good else None, covered_to=good[-1]["open_time"] if good else None)


def latest_open(conn, pair, interval):
    return conn.execute("SELECT MAX(open_time) FROM crypto_candles WHERE exchange=? AND market_type=? AND pair=? AND "
                        "interval_min=?", (EXCHANGE, MARKET, pair, interval)).fetchone()[0]


def fetch_kraken(pair, interval, conn, now_utc=None):
    """Recent confirmed candles for one pair and interval: at most 2 public requests
    (AssetPairs if the pair map is stale, then OHLC). Never retries. Returns what happened."""
    now, pair = now_utc or _utcnow(), pair.upper().replace("-", "/")
    if pair not in PAIRS or interval not in INTERVALS:
        return {"pair": pair, "interval": interval, "status": "ERROR", "requests": 0,
                "message": f"Supported pairs {', '.join(PAIRS)}; intervals {INTERVALS} minutes."}
    latest = latest_open(conn, pair, interval)
    if latest is not None and latest >= last_closed_open(now, interval):
        return _log(conn, pair, interval, "API", "kraken:OHLC", 0, "SKIPPED", now,
                    f"Already up to date (latest confirmed candle {iso(latest)}). Not sending.")
    requests = 0
    try:
        mapping, requests = pair_map(conn, now)
        info = mapping.get(pair)
        if not info:
            return _log(conn, pair, interval, "API", "kraken:AssetPairs", requests, "ERROR", now,
                        f"unknown_pair: Kraken AssetPairs does not list {pair}; nothing fetched or invented.")
        payload = _get("OHLC", {"pair": info["altname"], "interval": interval})
        requests += 1
        result = payload["result"]
        keys = [k for k in result if k != "last"]
        # We asked for exactly one pair. OHLC answers under Kraken's own pair id (e.g. XXBTZUSD), which differs from
        # the AssetPairs key when AssetPairs was queried by display name ("BTC/USD"). Accept the single key only if
        # it names the same base and quote; anything else is an unexpected answer.
        key = info["exchange_pair"] if info["exchange_pair"] in result else next(
            (k for k in keys if len(keys) == 1 and info["altname"][:3] in k and info["quote"] in k), None)
        series = result.get(key) if key else None
        if not isinstance(series, list):
            raise ProviderError("bad_response", f"No candles for {pair} in the answer (keys: {', '.join(keys)[:80]}).")
        if key != info["exchange_pair"]:
            info = {**info, "exchange_pair": key}
            with conn:
                conn.execute("UPDATE crypto_pairs SET exchange_pair=? WHERE exchange=? AND market_type=? AND pair=?",
                             (key, EXCHANGE, MARKET, pair))
    except ProviderError as err:
        log.warning("Kraken %s for %s %smin: %s", err.kind, pair, interval, err)
        return _log(conn, pair, interval, "API", "kraken:OHLC", requests, "ERROR", now, f"{err.kind}: {err}")
    names = ("time", "open", "high", "low", "close", "vwap", "volume", "trades")
    raw = [dict(zip(names, c)) if isinstance(c, list) else {} for c in series]
    # Kraken: "the last entry in the OHLC array is for the current, not-yet-committed timeframe".
    return _ingest(conn, info, interval, raw[:-1], "API", f"kraken:OHLC {info['altname']}", requests, now,
                   received=len(raw), unconfirmed=1 if raw else 0)


def import_csv(path, conn, pair, interval, now_utc=None):
    """Import Kraken's official OHLCVT download (no header: time, open, high, low, close, volume, trades)
    or a CSV with a header (time/timestamp, open, high, low, close, volume [, trades, vwap]). Times are Unix seconds.
    Rows from a file are labeled CSV and never treated as verified exchange data."""
    now, pair = now_utc or _utcnow(), pair.upper().replace("-", "/")
    if pair not in PAIRS or interval not in INTERVALS:
        raise ValueError(f"Supported pairs {', '.join(PAIRS)}; intervals {INTERVALS} minutes.")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError(f"CSV has more than {MAX_CSV_ROWS} rows.")
    head = [c.strip().lower() for c in rows[0]] if rows else []
    if head and not head[0].replace(".", "", 1).isdigit():  # header row
        head = ["time" if c in ("timestamp", "date", "time") else c for c in head]
        raw = [dict(zip(head, r)) for r in rows[1:]]
    else:                                                    # Kraken OHLCVT: no header
        raw = [dict(zip(("time", "open", "high", "low", "close", "volume", "trades"), r)) for r in rows]
    cached = conn.execute("SELECT * FROM crypto_pairs WHERE exchange=? AND market_type=? AND pair=?", (EXCHANGE, MARKET, pair)).fetchone()
    info = dict(cached) if cached else {"pair": pair, "exchange_pair": pair.replace("/", ""), "base": pair.split("/")[0],
                                         "quote": pair.split("/")[1]}
    return _ingest(conn, info, interval, raw, "CSV", f"csv:{Path(path).name}", 0, now, received=len(raw))


# ---------- Kraken OHLCVT archive (official full-history download) ----------
class _JoinedParts(io.RawIOBase):
    """Read-only, seekable view of a file split byte-by-byte into parts (X.zip.part00, part01, ...),
    so the ZIP can be read in place without writing a joined copy to disk."""

    def __init__(self, paths):
        super().__init__()
        self.files = [open(p, "rb") for p in paths]
        self.sizes = [os.path.getsize(p) for p in paths]
        self.starts = list(itertools.accumulate([0] + self.sizes[:-1]))
        self.size, self.pos = sum(self.sizes), 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, buf):
        view, done = memoryview(buf), 0
        while done < len(buf) and self.pos < self.size:
            i = bisect.bisect_right(self.starts, self.pos) - 1
            self.files[i].seek(self.pos - self.starts[i])
            chunk = self.files[i].read(min(len(buf) - done, self.sizes[i] - (self.pos - self.starts[i])))
            if not chunk:
                break
            view[done:done + len(chunk)] = chunk
            done += len(chunk)
            self.pos += len(chunk)
        return done

    def close(self):
        for f in self.files:
            f.close()
        super().close()


def open_archive(path):
    """ZipFile over a single .zip or over split parts (pass the folder or the first part)."""
    path = Path(path)
    parts = sorted(path.glob("*.zip.part*")) if path.is_dir() else sorted(path.parent.glob(path.name.split(".part")[0] + ".part*")) \
        if ".part" in path.name else [path]
    if not parts:
        raise FileNotFoundError(f"No .zip or .zip.partNN files at {path}")
    return zipfile.ZipFile(io.BufferedReader(_JoinedParts(parts), buffer_size=1 << 20)), parts


def archive_members(zf, conn):
    """{(pair, interval): member name} for our pairs and intervals, using Kraken's own altnames
    (XBTUSD_60.csv) from the AssetPairs map. Names that don't match exactly are ignored, never guessed."""
    alt = {r["altname"]: r["pair"] for r in conn.execute("SELECT pair, altname FROM crypto_pairs WHERE exchange=? AND market_type=?",
                                                          (EXCHANGE, MARKET))}
    found = {}
    for name in zf.namelist():
        stem = Path(name).name.rsplit(".", 1)[0]
        base, _, minutes = stem.rpartition("_")
        if base in alt and minutes.isdigit() and int(minutes) in INTERVALS:
            found[(alt[base], int(minutes))] = name
    return found


def import_archive(path, conn, pairs=PAIRS, intervals=None, batch=50_000, now_utc=None):
    """Stream the chosen CSVs out of Kraken's OHLCVT archive into crypto_candles in batches of `batch` rows.
    Each batch is one transaction; re-running only adds what is missing (stored rows are never overwritten)."""
    now, intervals = now_utc or _utcnow(), intervals or INTERVALS
    zf, parts = open_archive(path)
    results = []
    with zf:
        members = archive_members(zf, conn)
        for pair in pairs:
            info = dict(conn.execute("SELECT * FROM crypto_pairs WHERE pair=?", (pair,)).fetchone())
            for interval in intervals:
                name = members.get((pair, interval))
                if not name:
                    results.append({"pair": pair, "interval": interval, "status": "MISSING", "member": None})
                    continue
                total = {"pair": pair, "interval": interval, "member": name, "rows": 0, "inserted": 0, "unchanged": 0,
                         "conflicts": 0, "rejected": 0, "unconfirmed": 0, "out_of_order": 0, "skipped_already_imported": 0,
                         "first": None, "last": None}
                source = f"kraken-ohlcvt:{Path(parts[0]).name.split('.part')[0]}/{name}"
                # Resume point: batches commit one by one, so everything up to the newest row stored from this file is done.
                resume = conn.execute("SELECT MAX(open_time) FROM crypto_candles WHERE exchange=? AND market_type=? AND pair=? "
                                      "AND interval_min=? AND source=?", (EXCHANGE, MARKET, pair, interval, source)).fetchone()[0]
                prev = None
                with zf.open(name) as raw:
                    reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                    names = ("time", "open", "high", "low", "close", "volume", "trades")
                    while True:  # `batch` rows at a time, so even multi-GB 1-minute files never sit in memory
                        rows = [dict(zip(names, r)) for r in itertools.islice(reader, batch)]
                        if not rows:
                            break
                        for r in rows:  # the file should be in time order; count (don't fix) any step backwards
                            t = r.get("time", "")
                            if t.isdigit():
                                total["out_of_order"] += prev is not None and int(t) <= prev
                                prev = int(t)
                                total["first"] = total["first"] or prev
                        total["rows"] += len(rows)
                        fresh = [r for r in rows if not (resume and r.get("time", "").isdigit() and int(r["time"]) <= resume)]
                        total["skipped_already_imported"] += len(rows) - len(fresh)
                        if not fresh:
                            continue
                        res = _ingest(conn, info, interval, fresh, "CSV", source, 0, now, received=len(fresh))
                        for k in ("inserted", "unchanged", "conflicts", "rejected", "unconfirmed"):
                            total[k] += res.get(k) or 0
                        if res["status"] == "ERROR":
                            total["status"] = "ERROR"
                            break
                total["last"] = prev
                total.setdefault("status", "OK" if not total["rejected"] and not total["conflicts"] else "PARTIAL")
                # One window for the whole file: Kraken omits bars with no trades, so a gap inside it means "no trades".
                _log(conn, pair, interval, "CSV", f"kraken-ohlcvt:{name} (file summary)", 0, total["status"], now,
                     f"file summary: {total['rows']} rows, {total['out_of_order']} out of order", received=total["rows"],
                     inserted=total["inserted"], unchanged=total["unchanged"], conflicts=total["conflicts"],
                     rejected=total["rejected"], covered_from=total["first"], covered_to=total["last"])
                results.append(total)
                log.info("%s %smin: %s rows, %s new, %s same, %s rejected", pair, interval, total["rows"], total["inserted"],
                         total["unchanged"], total["rejected"])
    return results


def gap_report(conn, pair, interval):
    """Every gap in the stored series, streamed from the DB (works for millions of rows). Each gap is labeled:
    inside a window that a fetch or file covered -> no candle from Kraken (no trades); otherwise -> never collected."""
    step = interval * 60
    merged = []  # covered windows merged into disjoint ranges, so each gap is classified with one binary search
    for a, b in conn.execute("SELECT covered_from, covered_to FROM crypto_fetch_log WHERE pair=? AND interval_min=? "
                             "AND covered_from IS NOT NULL ORDER BY covered_from", (pair, interval)):
        if merged and a <= merged[-1][1] + step:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    starts = [m[0] for m in merged]
    gaps, prev = [], None
    for (t,) in conn.execute("SELECT open_time FROM crypto_candles WHERE exchange=? AND market_type=? AND pair=? AND interval_min=? "
                             "ORDER BY open_time", (EXCHANGE, MARKET, pair, interval)):
        if prev is not None and t - prev > step:
            start, end = prev + step, t - step
            k = bisect.bisect_right(starts, start) - 1
            inside = k >= 0 and end <= merged[k][1]
            gaps.append({"from": start, "to": end, "missing": (end - start) // step + 1,
                         "kind": "no_candle_from_exchange" if inside else "not_collected"})
        prev = t
    return gaps


# ---------- reading ----------
def load_candles(conn, pair, interval, limit=-1):
    """Stored candles oldest first; `limit` keeps only the most recent ones (-1 = all)."""
    rows = conn.execute("SELECT * FROM (SELECT * FROM crypto_candles WHERE exchange=? AND market_type=? AND pair=? AND interval_min=? "
                        "ORDER BY open_time DESC LIMIT ?) ORDER BY open_time", (EXCHANGE, MARKET, pair, interval, limit)).fetchall()
    return [{**dict(r), "ts": iso(r["open_time"])} for r in rows]


def quality(conn, pair, interval, candles, now_utc):
    """(warnings, gap counts, candles behind). Missing candles are classified, never filled in:
    REJECTED (we refused abnormal data), NO CANDLE FROM KRAKEN (inside a window Kraken answered: likely no trades),
    NOT COLLECTED (no successful fetch covered that time: network failure or fetch not run)."""
    if not candles:
        return ["DATA UNAVAILABLE: no stored candles for this pair and interval."], {}, None
    step, times = interval * 60, [c["open_time"] for c in candles]
    have = set(times)
    missing = [t for t in range(times[0], times[-1] + step, step) if t not in have]
    windows = conn.execute("SELECT covered_from, covered_to FROM crypto_fetch_log WHERE pair=? AND interval_min=? "
                           "AND covered_from IS NOT NULL", (pair, interval)).fetchall()
    refused = {r[0] for r in conn.execute("SELECT open_time FROM crypto_rejected WHERE pair=? AND interval_min=?", (pair, interval))}
    gaps = {"rejected": [], "no_candle_from_exchange": [], "not_collected": []}
    for t in missing:
        kind = "rejected" if t in refused else "no_candle_from_exchange" if any(a <= t <= b for a, b in windows) else "not_collected"
        gaps[kind].append(t)
    warnings = []
    for kind, label in (("rejected", "rejected as abnormal"), ("no_candle_from_exchange", "missing inside a window Kraken answered (Kraken sent no candle; likely no trades)"),
                        ("not_collected", "never collected (no successful fetch covered them: network failure or fetch not run)")):
        if gaps[kind]:
            warnings.append(f"{len(gaps[kind])} candle(s) {label}: {', '.join(iso(t) for t in gaps[kind][:4])}" + (" ..." if len(gaps[kind]) > 4 else ""))
    zero = [c["ts"] for c in candles if c["trades"] == 0]
    if zero:
        warnings.append(f"{len(zero)} candle(s) with zero trades (price carried over): {', '.join(zero[:4])}")
    behind = max(0, (last_closed_open(now_utc, interval) - times[-1]) // step)
    if behind:
        warnings.append(f"DELAYED: the latest stored candle ({candles[-1]['ts']}) is {behind} candle(s) "
                        f"({behind * interval / 60:.1f} h) behind the last closed Kraken candle. Run a fetch to update.")
    if any(c["origin"] == "CSV" for c in candles):
        warnings.append("Some candles came from a CSV file: not verified by this system.")
    return warnings, {k: len(v) for k, v in gaps.items()}, behind


def _status_label(candles):
    if not candles:
        return "DATA UNAVAILABLE"
    origins = {c["origin"] for c in candles}
    return "HISTORICAL · KRAKEN SPOT" if origins == {"API"} else "CSV · UNVERIFIED" if origins == {"CSV"} else "MIXED · PARTLY UNVERIFIED"


def detail(pair, interval, now_utc=None):
    """Everything the Crypto screen shows for one pair + interval. Read-only; never contacts Kraken."""
    now = now_utc or _utcnow()
    with contextlib.closing(connect()) as conn:
        candles = load_candles(conn, pair, interval, limit=5000)  # screen + warnings use the recent window; history can be millions
        total, first = conn.execute("SELECT COUNT(*), MIN(open_time) FROM crypto_candles WHERE exchange=? AND market_type=? "
                                    "AND pair=? AND interval_min=?", (EXCHANGE, MARKET, pair, interval)).fetchone()
        warnings, gaps, behind = quality(conn, pair, interval, candles, now)
        info = conn.execute("SELECT * FROM crypto_pairs WHERE pair=?", (pair,)).fetchone()
        log_rows = [dict(r) for r in conn.execute("SELECT * FROM crypto_fetch_log WHERE pair=? AND interval_min=? "
                                                  "ORDER BY id DESC LIMIT 5", (pair, interval))]
    closes, shown = [c["close"] for c in candles], candles[-CHART_CANDLES:]
    last_close_time = iso(candles[-1]["open_time"] + interval * 60) if candles else None
    context = {"market_type": MARKET, "exchange": EXCHANGE, "pair": pair, "interval": f"{interval}m",
               "analysis_time": last_close_time, "last_close": closes[-1] if closes else None,
               "data_source": _status_label(candles), "data_quality": "OK" if not warnings else f"{len(warnings)} warning(s)",
               "delayed": bool(behind), "behind_candles": behind}
    grok_state = grok.status()["state"]
    return {
        "pair": pair, "exchange": EXCHANGE, "market_type": MARKET, "interval": interval,
        "base": pair.split("/")[0], "quote": pair.split("/")[1],
        "exchange_pair": info["exchange_pair"] if info else None, "pair_status": info["status"] if info else None,
        "pair_verified_at": info["verified_at"] if info else None,
        "status": _status_label(candles), "rows": total, "window_rows": len(candles),
        "first": iso(first) if first else None, "last": candles[-1]["ts"] if candles else None,
        "last_close_time": last_close_time, "last_collected": max((c["collected_at"] for c in candles), default=None),
        "candles": [{k: c[k] for k in ("ts", "open", "high", "low", "close", "volume", "trades", "origin")} for c in shown],
        "sma20": indicators.sma_series(closes, 20)[-CHART_CANDLES:], "ema12": indicators.ema_series(closes, 12)[-CHART_CANDLES:],
        "analysis": market_analyst.market_report(pair, candles, "crypto", context),
        "warnings": warnings, "gaps": gaps, "behind_candles": behind, "log": log_rows,
        "grok": "GROK CONNECTED · research not run" if grok_state == "CONNECTED" else f"GROK OFFLINE ({grok_state})",
        "note": "HISTORICAL confirmed candles, not real-time. Display/research only: never used by the trading simulation.",
    }


def overview(now_utc=None):
    now = now_utc or _utcnow()
    out = []
    with contextlib.closing(connect()) as conn:
        for pair in PAIRS:
            for interval in INTERVALS:
                n, last = conn.execute("SELECT COUNT(*), MAX(open_time) FROM crypto_candles WHERE exchange=? AND market_type=? "
                                       "AND pair=? AND interval_min=?", (EXCHANGE, MARKET, pair, interval)).fetchone()
                out.append({"pair": pair, "interval": interval, "rows": n, "last": iso(last) if last else None,
                            "status": "HISTORICAL" if n else "DATA UNAVAILABLE",
                            "delayed": bool(n) and last < last_closed_open(now, interval)})
    return out


# ---------- command line ----------
def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-8s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(prog="python -m app.cryptodata", description="Kraken spot candles (research only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch", help="Kraken public OHLC; 1 request per pair (+1 AssetPairs if the pair map is stale)")
    fetch.add_argument("pairs", nargs="*", default=list(PAIRS))
    fetch.add_argument("--interval", type=int, default=60, choices=INTERVALS)
    imp = sub.add_parser("import-csv", help="Kraken OHLCVT download or your own CSV")
    imp.add_argument("file")
    imp.add_argument("--pair", required=True)
    imp.add_argument("--interval", type=int, default=60, choices=INTERVALS)
    arc = sub.add_parser("import-archive", help="Kraken OHLCVT full-history ZIP (single .zip or split .zip.partNN parts)")
    arc.add_argument("path", help="the .zip, its first .part file, or the folder holding the parts")
    arc.add_argument("--pairs", nargs="*", default=list(PAIRS))
    arc.add_argument("--intervals", nargs="*", type=int, default=list(INTERVALS), choices=INTERVALS)
    sub.add_parser("status", help="what is stored")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(overview(), indent=2))
        return
    with contextlib.closing(connect()) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("data/market_data.db failed its integrity check. Not writing to it.")
        if args.cmd == "fetch":
            for i, pair in enumerate(args.pairs):
                result = fetch_kraken(pair, args.interval, conn)
                print(json.dumps(result))
                if result["requests"] and i < len(args.pairs) - 1:
                    time.sleep(GAP_SECONDS)
        elif args.cmd == "import-archive":
            for result in import_archive(args.path, conn, [p.upper().replace("-", "/") for p in args.pairs], args.intervals):
                print(json.dumps(result))
        else:
            print(json.dumps(import_csv(Path(args.file), conn, args.pair, args.interval)))


if __name__ == "__main__":
    main()
