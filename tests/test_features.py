"""Feature preparation: formulas, no look-ahead, higher-timeframe alignment, gap flags and time splits."""

import statistics

from app import features, indicators

H, D = 3600, 86400


def hourly(n, start=1_700_000_000 // D * D, missing=()):
    """(open_time, open, high, low, close, volume) hourly rows with a wavy price; `missing` indexes are left out."""
    rows = []
    for i in range(n):
        c = 100 + 5 * ((i * 7) % 13) / 13 + i * 0.05
        rows.append((start + i * H, c - 0.2, c + 1.0, c - 1.0, c, 10 + (i % 5)))
    return [r for i, r in enumerate(rows) if i not in missing]


def daily_from(rows):
    days = {}
    for t, o, h, l, c, v in rows:
        d = t // D * D
        o0, h0, l0, _, v0 = days.get(d, (o, h, l, c, 0))
        days[d] = (o0, max(h0, h), min(l0, l), c, v0 + v)
    return [(d, *vals) for d, vals in sorted(days.items())]


def test_features_never_use_future_candles():
    rows = hourly(1200)
    full = features.compute(rows, 60, {1440: daily_from(rows)})
    cut = 900
    part_rows = rows[:cut]
    part = features.compute(part_rows, 60, {1440: daily_from(rows)})   # same higher-TF data, fewer base candles
    for name in features.FEATURES:
        assert part[name] == full[name][:cut], name


def test_higher_timeframe_trend_uses_only_closed_daily_candles():
    rows = hourly(24 * 40)
    days = daily_from(rows)
    base = features.compute(rows, 60, {1440: days})
    i = 24 * 30 + 5                                                     # 05:00-06:00 on day 30
    tampered = list(days)
    tampered[30] = (days[30][0], 1, 1e9, 1, 1e9, 1)                     # day 30 is still open at that time
    tampered[31:] = [(d[0], 1, 1, 1, 1, 1) for d in days[31:]]         # and every later day
    after = features.compute(rows, 60, {1440: tampered})
    assert after["trend_1440"][:i + 1] == base["trend_1440"][:i + 1]


def test_formulas_match_definitions():
    rows = hourly(200)
    cols = features.compute(rows, 60)
    close = [r[4] for r in rows]
    assert round(cols["rsi_14"][-1], 2) == indicators.rsi(close)
    m = indicators.macd(close)
    assert (round(cols["macd"][-1], 4), round(cols["macd_signal"][-1], 4)) == (m["macd"], m["signal"])
    rets = [(close[i] / close[i - 1] - 1) * 100 for i in range(len(close) - 20, len(close))]
    assert abs(cols["volatility_20"][-1] - statistics.pstdev(rets)) < 1e-9
    assert abs(cols["momentum_19"][-1] - (close[-1] / close[-20] - 1) * 100) < 1e-12
    flat = [(i * H, 10.0, 11.0, 9.0, 10.0, 1.0) for i in range(30)]   # every true range is 2
    assert indicators.atr_series([r[2] for r in flat], [r[3] for r in flat], [r[4] for r in flat])[14:] == [2.0] * 16
    assert indicators.atr_series([1.0] * 10, [1.0] * 10, [1.0] * 10)[-1] is None   # too few candles: nothing invented
    assert cols["sma_20"][18] is None and cols["macd_signal"][32] is None and cols["macd_signal"][33] is not None


def test_chunked_build_equals_full_history_and_uses_every_higher_timeframe(tmp_path, monkeypatch):
    import contextlib
    import datetime as dt
    from app import cryptodata, stockdata
    monkeypatch.setattr(stockdata, "DB_PATH", tmp_path / "f.db")
    monkeypatch.setattr(features, "WARMUP_CANDLES", 400)
    rows = hourly(24 * 60)
    now = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    with contextlib.closing(cryptodata.connect()) as conn:
        for interval, data in ((60, rows), (1440, daily_from(rows)), (240, [r for r in rows if r[0] % (4 * H) == 0])):
            path = tmp_path / f"{interval}.csv"
            path.write_text("\n".join(",".join(map(str, r)) for r in data) + "\n", encoding="utf-8")
            assert cryptodata.import_csv(path, conn, "BTC/USD", interval, now)["rejected"] == 0
        full = features.build(conn, "BTC/USD", 60)
        start, end = rows[900][0], rows[1100][0]
        chunk = features.build(conn, "BTC/USD", 60, start, end)
        assert chunk["open_time"] == full["open_time"][900:1101]
        for name in features.FEATURES:
            for a, b in zip(chunk[name], full[name][900:1101]):
                assert (a is None and b is None) or abs(a - b) < 1e-6, name
        assert all(v is not None for v in chunk["trend_240"]) and all(v is not None for v in chunk["trend_1440"])
        s = features.summarize(conn, "BTC/USD", 60, embargo=24, chunk=500)
        assert s["rows"] == len(rows) and "trend_240" in s["features"] and "trend_15" not in s["features"]


def test_sparse_series_rows_are_flagged_not_filled():
    rows = hourly(80, missing={50})
    cols = features.compute(rows, 60)
    assert len(cols["open_time"]) == 79                                 # nothing filled in
    assert cols["window_has_gap"][49 + 25] is True and cols["window_has_gap"][-1] is False


def test_time_split_is_ordered_with_embargo_and_labels_stay_separate():
    times = [i * H for i in range(1000)]
    s = features.time_split(times, embargo=24)
    (a0, a1), (b0, b1), (c0, c1) = s["train"], s["valid"], s["test"]
    assert a0 == 0 and a1 <= b0 - 24 and b1 <= c0 - 24 and c1 == 1000
    assert times[a1 - 1] < times[b0] < times[b1 - 1] < times[c0]
    label = features.label_forward_return([float(i + 1) for i in range(10)], 3)
    assert label[-3:] == [None] * 3 and label[0] == (4 / 1 - 1) * 100
    assert "label" not in " ".join(features.FEATURES)
