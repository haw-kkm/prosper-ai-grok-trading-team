"""US stock market (NYSE / Nasdaq) trading days, rule-based. Early closes don't matter for daily bars.

shortcut: holiday rules + a short list of unscheduled closures; add a new special closure to
SPECIAL_CLOSURES when the exchange announces one.
"""

import datetime as dt

SPECIAL_CLOSURES = {dt.date(2018, 12, 5), dt.date(2025, 1, 9)}  # national days of mourning (G.H.W. Bush, Carter)
FINAL_AFTER_UTC_HOUR = 22  # a session's daily bar counts as final from 22:00 UTC (16:00 ET close + at least 1h)


def _nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(day):
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    return day - dt.timedelta(days=1) if day.weekday() == 5 else day + dt.timedelta(days=1) if day.weekday() == 6 else day


def holidays(year):
    days = {
        _nth_weekday(year, 1, 0, 3),               # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),               # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),                 # Memorial Day
        _observed(dt.date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),               # Labor Day
        _nth_weekday(year, 11, 3, 4),              # Thanksgiving
        _observed(dt.date(year, 12, 25)),          # Christmas
    }
    if dt.date(year, 1, 1).weekday() != 5:         # a Saturday New Year's Day is not moved to Dec 31
        days.add(_observed(dt.date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(dt.date(year, 6, 19)))  # Juneteenth
    return days | {d for d in SPECIAL_CLOSURES if d.year == year}


def is_trading_day(day):
    return day.weekday() < 5 and day not in holidays(day.year)


def trading_days(start, end):
    """Trading days from start to end, inclusive."""
    out, day = [], start
    while day <= end:
        if is_trading_day(day):
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def bar_is_final(day, now_utc):
    """True once the session on `day` has closed, so its daily bar won't change any more."""
    return now_utc.replace(tzinfo=None) >= dt.datetime(day.year, day.month, day.day, FINAL_AFTER_UTC_HOUR)


def session_open(now_utc):
    """True during the NYSE regular session, 09:30-16:00 New York time, on a trading day.

    shortcut: early-close days (13:00 ET) count as full sessions and the DST switch is taken at midnight UTC, not 2am local;
    upgrade with the tzdata package + zoneinfo if stock orders ever need minute accuracy.
    """
    year, day = now_utc.year, now_utc.date()
    daylight = _nth_weekday(year, 3, 6, 2) <= day < _nth_weekday(year, 11, 6, 1)  # 2nd Sunday of March to 1st Sunday of November
    local = now_utc.replace(tzinfo=None) - dt.timedelta(hours=4 if daylight else 5)
    return is_trading_day(local.date()) and dt.time(9, 30) <= local.time() < dt.time(16, 0)


def last_completed_session(now_utc):
    day = now_utc.date()
    while not (is_trading_day(day) and bar_is_final(day, now_utc)):
        day -= dt.timedelta(days=1)
    return day
