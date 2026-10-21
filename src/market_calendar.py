"""US equity session dates: which daily bar is final at a given moment."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
# Regular close is 16:00 ET; bars are treated as final ten minutes later.
SESSION_FINAL_TIME = time(16, 10)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    """Regular NYSE full-day holidays. One-off closures (e.g. national mourning) are not included."""
    holidays = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        holidays.add(new_year + timedelta(days=1))
    elif new_year.weekday() < 5:
        holidays.add(new_year)  # NYSE does not observe a Saturday New Year's Day on Friday.
    holidays.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    holidays.add(_easter(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))  # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    holidays.add(_observed(date(year, 12, 25)))  # Christmas
    return frozenset(holidays)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def previous_trading_day(day: date) -> date:
    current = day - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def expected_session(now: datetime) -> date:
    """Latest session whose daily bar is final at ``now`` (timezone-aware)."""
    local = now.astimezone(NEW_YORK)
    today = local.date()
    if is_trading_day(today) and local.time() >= SESSION_FINAL_TIME:
        return today
    return previous_trading_day(today)
