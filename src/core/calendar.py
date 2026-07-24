"""A-share trading calendar backed by ``chinese_calendar``.

``chinese_calendar`` is a community-maintained library that tracks
沪深交易所 holiday schedules (including 调休 workday weekends).
It is updated annually — run ``pip install --upgrade chinese_calendar``
each December to pick up the following year's schedule.

For years beyond the library's data range a ``NotImplementedError``
is raised; we fall back to a weekday-only check and emit a loud
warning so the operator knows to upgrade.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from chinese_calendar import is_workday

_logger = logging.getLogger(__name__)

# A-share continuous trading session windows (Beijing time).
_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_END = time(15, 0)

# Track whether we've already warned about unsupported years to
# avoid flooding the logs.
_warned_years: set[int] = set()


def is_trading_day(day: date) -> bool:
    """Return True if *day* is a regular A-share trading day.

    Delegates to ``chinese_calendar.is_workday`` which accounts for
    weekends, public holidays, *and* 调休 weekend workdays.

    For years beyond the library's supported range (currently
    2004–2026) we fall back to a plain weekday check and emit a
    one-time warning per calendar year.
    """
    try:
        return bool(is_workday(day))
    except NotImplementedError:
        if day.year not in _warned_years:
            _warned_years.add(day.year)
            _logger.warning(
                "chinese_calendar has no data for %d — falling back to "
                "weekday-only check.  Run `pip install --upgrade chinese_calendar` "
                "to pick up the latest holiday schedule.",
                day.year,
            )
        return day.weekday() < 5


def is_trading_time(dt: datetime | None = None) -> bool:
    """Return True if *dt* falls within continuous trading hours
    (9:30–11:30 or 13:00–15:00 Beijing time), excluding the
    pre-market call-auction period (9:15–9:25).
    """
    now = dt or datetime.now()
    t = now.time()
    if _MORNING_START <= t <= _MORNING_END:
        return True
    if _AFTERNOON_START <= t <= _AFTERNOON_END:
        return True
    return False


def is_realtime_available(dt: datetime | None = None) -> bool:
    """Return True if real-time quotes are meaningful at *dt*.

    Unlike ``is_trading_time`` this treats the trading day as one
    continuous window (9:30–15:00 Beijing time) — the midday lunch
    break (11:30–13:00) is INCLUDED, because quotes fetched during
    the break still reflect the morning session's latest state and
    make a valid intraday snapshot.

    Use this to gate intraday / real-time data features; keep using
    ``is_trading_time`` where actual continuous-auction sessions
    matter.
    """
    now = dt or datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return _MORNING_START <= t <= _AFTERNOON_END


def is_past_market_open(dt: datetime | None = None) -> bool:
    """Return True if *dt* is a trading day at or past the 9:30 open.

    Unlike ``is_realtime_available`` this has no 15:00 upper bound — it
    stays True after the close, which is exactly the window where the
    daily-bar write job may not have persisted today's bar yet and an
    intraday snapshot must be synthesized from live quotes.

    Use this to gate "today's bar must be present" overlays; keep using
    ``is_realtime_available`` where only live-session quotes matter.
    """
    now = dt or datetime.now()
    if not is_trading_day(now.date()):
        return False
    return now.time() >= _MORNING_START


def previous_trading_day(day: date | None = None) -> date:
    """Return the most recent trading day on or before *day*.

    Walks backwards day-by-day; acceptable because the search
    distance is never more than ~10 calendar days (longest
    holiday break).
    """
    cursor = day or date.today()
    # Walk back at most 20 calendar days.
    for _ in range(20):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    # Fallback — should never be reached for realistic inputs.
    return cursor


def next_trading_day(day: date | None = None) -> date:
    """Return the earliest trading day on or after *day*."""
    cursor = day or date.today()
    for _ in range(20):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def trading_session_status(now: datetime | None = None) -> dict:
    """Convenience helper for API endpoints.

    Returns a dict with keys:
      is_trading_day, is_trading_time, is_realtime_available, next_session
    where *next_session* is a human-readable string.
    """
    dt = now or datetime.now()
    today = dt.date()
    trading_day = is_trading_day(today)
    trading_time = is_trading_time(dt) if trading_day else False
    realtime_available = is_realtime_available(dt)

    if trading_time:
        next_session = "in_session"
    elif trading_day:
        if dt.time() < _MORNING_START:
            next_session = f"今日 {_MORNING_START.strftime('%H:%M')} 开盘"
        elif dt.time() < _AFTERNOON_START:
            next_session = f"午间休盘，今日 {_AFTERNOON_START.strftime('%H:%M')} 开盘"
        else:
            next_session = "今日已收盘"
    else:
        nxt = next_trading_day(today + timedelta(days=1) if dt.time() >= _AFTERNOON_END else today)
        next_session = f"{nxt.isoformat()} 开盘"

    return {
        "is_trading_day": trading_day,
        "is_trading_time": trading_time,
        "is_realtime_available": realtime_available,
        "next_session": next_session,
    }
