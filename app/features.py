"""Model-ready features from stored Kraken spot candles. PREPARATION ONLY: nothing here trains a model or trades.

    python -m app.features summary --pair BTC/USD --interval 60
    python -m app.features export  --pair BTC/USD --interval 60 --out data/features_BTCUSD_60.csv

No look-ahead:
- every feature on candle i uses only candles 0..i (its own close included, nothing after it);
- higher-timeframe trend on candle i uses only higher-timeframe candles that had CLOSED by the time candle i closed;
- labels (future returns) come from label_forward_return(), are kept apart, and never feed a feature;
- time_split() orders train < validation < test in time with an embargo gap so labels can't overlap the next set.
Kraken omits candles with no trades, so the series is sparse: `window_has_gap` marks rows whose 26-candle window
spans more time than 26 consecutive candles. Missing candles are never filled in.
"""

import argparse
import contextlib
import csv
import math

from . import indicators
from .cryptodata import EXCHANGE, MARKET, PAIRS, connect, iso

HTF = (5, 15, 60, 240, 1440)  # timeframes that can add a trend_* column to a lower-timeframe series
FEATURES = ("sma_5", "sma_20", "ema_12", "ema_26", "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14",
            "volatility_20", "momentum_19", "volume_change_20") + tuple(f"trend_{h}" for h in HTF)
WARMUP_CANDLES = 5000  # a chunk is computed with this many earlier candles in front: older EMA/RSI/ATR weight < 1e-100
CHUNK = 500_000        # candles per chunk for summary/export, so 1-minute history never sits in memory at once


def _rolling_std_of_returns(closes, n=20):
    out, rets, s, sq = [None] * len(closes), [None], 0.0, 0.0
    for i in range(1, len(closes)):
        r = (closes[i] / closes[i - 1] - 1) * 100
        rets.append(r)
        s, sq = s + r, sq + r * r
        if i > n:
            old = rets[i - n]
            s, sq = s - old, sq - old * old
        if i >= n:
            out[i] = math.sqrt(max(sq / n - (s / n) ** 2, 0.0))
    return out


def _trend_series(closes):
    """+1 when SMA 5 > SMA 20, -1 when below, None before 20 closes (same rule as the Market Analyst)."""
    fast, slow = indicators.sma_series(closes, 5), indicators.sma_series(closes, 20)
    return [None if s is None else (1 if f > s else -1) for f, s in zip(fast, slow)]


def _align_higher(base_close_times, htf_close_times, htf_values):
    """Value of the latest higher-timeframe candle that had closed at or before each base candle closed."""
    out, j = [], -1
    for t in base_close_times:
        while j + 1 < len(htf_close_times) and htf_close_times[j + 1] <= t:
            j += 1
        out.append(htf_values[j] if j >= 0 else None)
    return out


def _load(conn, pair, interval, start=None, end=None, warmup=0):
    """Candles in [start, end] (all when None), plus `warmup` earlier candles in front."""
    q = ("SELECT open_time, open, high, low, close, volume FROM crypto_candles WHERE exchange=? AND market_type=? "
         "AND pair=? AND interval_min=?")
    args = [EXCHANGE, MARKET, pair, interval]
    before = conn.execute(q + " AND open_time < ? ORDER BY open_time DESC LIMIT ?", (*args, start, warmup)).fetchall()[::-1] \
        if start is not None and warmup else []
    if start is not None:
        q, args = q + " AND open_time >= ?", args + [start]
    if end is not None:
        q, args = q + " AND open_time <= ?", args + [end]
    return before + conn.execute(q + " ORDER BY open_time", args).fetchall()


def compute(rows, interval, higher=None):
    """Columns {name: [values]} for candles `rows` (open_time, open, high, low, close, volume), oldest first.
    `higher` = {interval_min: rows} of higher-timeframe candles for the trend_* columns."""
    t = [r[0] for r in rows]
    high, low, close, vol = [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows]
    step = interval * 60
    line, sig, hist = indicators.macd_series(close)
    cols = {
        "open_time": t, "close": close,
        "sma_5": indicators.sma_series(close, 5), "sma_20": indicators.sma_series(close, 20),
        "ema_12": indicators.ema_series(close, 12), "ema_26": indicators.ema_series(close, 26),
        "rsi_14": indicators.rsi_series(close, 14), "macd": line, "macd_signal": sig, "macd_hist": hist,
        "atr_14": indicators.atr_series(high, low, close, 14), "volatility_20": _rolling_std_of_returns(close, 20),
        "momentum_19": [None if i < 19 else (close[i] / close[i - 19] - 1) * 100 for i in range(len(close))],
        "window_has_gap": [i < 25 or t[i] - t[i - 25] != 25 * step for i in range(len(t))],
    }
    vc, s = [None] * len(vol), 0.0
    for i, v in enumerate(vol):  # latest volume vs the average of the 20 candles before it
        if i >= 20 and s > 0:
            vc[i] = (v / (s / 20) - 1) * 100
        s += v - (vol[i - 20] if i >= 20 else 0.0)
    cols["volume_change_20"] = vc
    close_times = [x + step for x in t]
    for h in HTF:
        hrows = (higher or {}).get(h)
        if h > interval and hrows:
            cols[f"trend_{h}"] = _align_higher(close_times, [r[0] + h * 60 for r in hrows], _trend_series([r[4] for r in hrows]))
        else:
            cols[f"trend_{h}"] = [None] * len(t)  # not a higher timeframe for this series (or no data)
    return cols


def build(conn, pair, interval, start=None, end=None):
    """Features for one stored series (or only its candles in [start, end]) with trends from every stored higher
    timeframe. A chunk is computed with WARMUP_CANDLES earlier candles in front that are then dropped, so chunked
    results equal a full-history run; each higher timeframe is loaded with 100 extra candles for its SMA 20."""
    rows = _load(conn, pair, interval, start, end, WARMUP_CANDLES)
    if not rows:
        return compute([], interval)
    higher = {h: _load(conn, pair, h, rows[0][0] - 100 * h * 60, end) for h in HTF if h > interval}
    cols = compute(rows, interval, higher)
    skip = sum(1 for r in rows if start is not None and r[0] < start)
    return {k: v[skip:] for k, v in cols.items()}


def label_forward_return(closes, horizon):
    """LABEL ONLY (uses the future by definition): return from close i to close i + horizon. Never a feature."""
    return [None if i + horizon >= len(closes) else (closes[i + horizon] / closes[i] - 1) * 100 for i in range(len(closes))]


def time_split(open_times, train=0.70, valid=0.15, embargo=0):
    """Index ranges for train / validation / test, in time order, split by TIME span (not shuffled).
    `embargo` candles are dropped after train and after validation so labels never reach into the next set."""
    if not open_times:
        return {"train": (0, 0), "valid": (0, 0), "test": (0, 0)}
    span = open_times[-1] - open_times[0]
    t1, t2 = open_times[0] + span * train, open_times[0] + span * (train + valid)
    i1 = next((i for i, x in enumerate(open_times) if x > t1), len(open_times))
    i2 = next((i for i, x in enumerate(open_times) if x > t2), len(open_times))
    return {"train": (0, i1), "valid": (min(i1 + embargo, i2), i2), "test": (min(i2 + embargo, len(open_times)), len(open_times))}


def _chunks(conn, pair, interval, chunk=CHUNK):
    """(all open times, [(start, end), ...]) covering the series in chunks of `chunk` candles."""
    times = [r[0] for r in conn.execute("SELECT open_time FROM crypto_candles WHERE exchange=? AND market_type=? AND pair=? "
                                        "AND interval_min=? ORDER BY open_time", (EXCHANGE, MARKET, pair, interval))]
    return times, [(times[a], times[min(a + chunk, len(times)) - 1]) for a in range(0, len(times), chunk)]


def summarize(conn, pair, interval, embargo=0, chunk=CHUNK):
    """Readiness of every feature over the whole series, computed chunk by chunk (bounded memory)."""
    times, chunks = _chunks(conn, pair, interval, chunk)
    stored = {h for (h,) in conn.execute("SELECT DISTINCT interval_min FROM crypto_candles WHERE pair=?", (pair,))}
    active = [f for f in FEATURES if not f.startswith("trend_") or (int(f[6:]) > interval and int(f[6:]) in stored)]
    ready = clean = 0
    first_ready = None
    for start, end in chunks:
        cols = build(conn, pair, interval, start, end)
        for i in range(len(cols["open_time"])):
            if all(cols[f][i] is not None for f in active):
                ready += 1
                clean += not cols["window_has_gap"][i]
                first_ready = first_ready or cols["open_time"][i]
    split = time_split(times, embargo=embargo)
    return {"rows": len(times), "first": iso(times[0]) if times else None, "last": iso(times[-1]) if times else None,
            "features": active, "feature_ready_rows": ready, "ready_without_gaps": clean,
            "first_ready": iso(first_ready) if first_ready else None,
            "split": {k: {"rows": b - a, "from": iso(times[a]) if b > a else None, "to": iso(times[b - 1]) if b > a else None}
                      for k, (a, b) in split.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.features", description="Feature preparation (no training).")
    parser.add_argument("cmd", choices=["summary", "export"])
    parser.add_argument("--pair", default="BTC/USD", choices=PAIRS)
    parser.add_argument("--interval", type=int, default=60, choices=(1, 5, 15, 60, 240, 1440))
    parser.add_argument("--embargo", type=int, default=24, help="candles dropped between train/valid/test")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    with contextlib.closing(connect()) as conn:
        if args.cmd == "summary":
            import json
            print(json.dumps({"pair": args.pair, "interval": args.interval, **summarize(conn, args.pair, args.interval, args.embargo)}, indent=2))
            return
        times, chunks = _chunks(conn, args.pair, args.interval)
        bounds = time_split(times, embargo=args.embargo)
        set_of = lambda i: next((k for k, (a, b) in bounds.items() if a <= i < b), "embargo")  # noqa: E731
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["open_time", "time_utc", "close", *FEATURES, "window_has_gap", "set"])
            i = 0
            for start, end in chunks:
                cols = build(conn, args.pair, args.interval, start, end)
                for j, t in enumerate(cols["open_time"]):
                    w.writerow([t, iso(t), cols["close"][j], *[cols[k][j] for k in FEATURES], int(cols["window_has_gap"][j]), set_of(i)])
                    i += 1
    print(f"wrote {i} rows to {args.out}")


if __name__ == "__main__":
    main()
