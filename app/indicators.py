"""Extra indicators for daily closes (oldest first): SMA series, RSI and MACD.

Every value at position i uses closes[:i + 1] only, so nothing peeks at future bars.
Too little data returns INSUFFICIENT instead of a made-up number.
"""

INSUFFICIENT = "INSUFFICIENT DATA"


def sma_series(values, n):
    """Trailing n-period average at each position; None until n values exist."""
    return [None if i + 1 < n else sum(values[i + 1 - n:i + 1]) / n for i in range(len(values))]


def ema_series(values, n):
    """Exponential average seeded with the simple average of the first n values; None before that."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = sum(values[:n]) / n
    for i in range(n, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def ema(values, n):
    """Latest n-period EMA, or INSUFFICIENT when there are fewer than n values."""
    return round(ema_series(values, n)[-1], 4) if len(values) >= n else INSUFFICIENT


def rsi_series(closes, n=14):
    """Wilder's RSI at each position; None until n + 1 closes exist."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    moves = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(m, 0) for m in moves[:n]) / n
    loss = sum(max(-m, 0) for m in moves[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            m = moves[i - 1]
            gain, loss = (gain * (n - 1) + max(m, 0)) / n, (loss * (n - 1) + max(-m, 0)) / n
        out[i] = (100.0 if gain > 0 else 50.0) if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def rsi(closes, n=14):
    """Wilder's RSI of the latest close. Needs n + 1 closes."""
    last = rsi_series(closes, n)[-1] if closes else None
    return INSUFFICIENT if last is None else round(last, 2)


def macd_series(closes, fast=12, slow=26, signal=9):
    """(MACD line, signal line, histogram) at each position; None until enough closes exist."""
    fast_ema, slow_ema = ema_series(closes, fast), ema_series(closes, slow)
    line = [None if s is None else f - s for f, s in zip(fast_ema, slow_ema)]
    sig = [None] * (slow - 1) + ema_series(line[slow - 1:], signal) if len(closes) >= slow else [None] * len(closes)
    hist = [None if s is None else m - s for m, s in zip(line, sig)]
    return line, sig, hist


def macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line and histogram of the latest close. Needs slow + signal - 1 closes."""
    if len(closes) < slow + signal - 1:
        return INSUFFICIENT
    line, sig, hist = macd_series(closes, fast, slow, signal)
    return {"macd": round(line[-1], 4), "signal": round(sig[-1], 4), "histogram": round(hist[-1], 4)}


def atr_series(highs, lows, closes, n=14):
    """Wilder's Average True Range at each position; None until n + 1 candles exist."""
    out = [None] * len(closes)
    tr = [None] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    if len(closes) < n + 1:
        return out
    atr = sum(tr[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, len(closes)):
        atr = (atr * (n - 1) + tr[i]) / n
        out[i] = atr
    return out
