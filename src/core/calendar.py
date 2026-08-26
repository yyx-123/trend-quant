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
from zoneinfo import ZoneInfo

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

# Cached market-local timezone (from app settings; Asia/Shanghai fallback).
_market_tz: ZoneInfo | None = None


def market_now() -> datetime:
    """Current wall-clock time in the market timezone (tz-aware).

    All session gates below are defined against Beijing time, so the
    default "now" must come from the configured market timezone rather
    than the host's local timezone (the two differ on non-CN hosts).
    """
    global _market_tz
    if _market_tz is None:
        tz_name = "Asia/Shanghai"
        try:
            from core.settings import load_settings

            tz_name = load_settings().app.timezone or tz_name
        except Exception:  # config unreadable — stick with the A-share default
            pass
        try:
            _market_tz = ZoneInfo(tz_name)
        except Exception:
            _market_tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(_market_tz)


def is_trading_day(day: date) -> bool:
    """Return True if *day* is a regular A-share trading day.

    Delegates to ``chinese_calendar.is_workday`` which accounts for
    weekends, public holidays, *and* 调休 weekend workdays — but the
    exchanges never open on a Saturday/Sunday even when it is an
    official workday, so weekends are always excluded.

    For years beyond the library's supported range (currently
    2004–2026) we fall back to a plain weekday check and emit a
    one-time warning per calendar year.
    """
    if day.weekday() >= 5:
        return False
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


def is_continuous_auction_hours(dt: datetime | None = None) -> bool:
    """Return True if *dt* falls within continuous trading hours
    (9:30–11:30 or 13:00–15:00 Beijing time), excluding the
    pre-market call-auction period (9:15–9:25).

    注意：本函数只看时间窗、不校验交易日（原名 is_trading_time 易误导，
    P2-31 改名明示）；需要交易日门控请组合 is_trading_day 或直接用
    is_realtime_available / is_past_market_open。
    """
    now = dt or market_now()
    t = now.time()
    if _MORNING_START <= t <= _MORNING_END:
        return True
    return _AFTERNOON_START <= t <= _AFTERNOON_END


def is_realtime_available(dt: datetime | None = None) -> bool:
    """Return True if real-time quotes are meaningful at *dt*.

    Unlike ``is_continuous_auction_hours`` this treats the trading day as one
    continuous window (9:30–15:00 Beijing time) — the midday lunch
    break (11:30–13:00) is INCLUDED, because quotes fetched during
    the break still reflect the morning session's latest state and
    make a valid intraday snapshot.

    Use this to gate intraday / real-time data features; keep using
    ``is_continuous_auction_hours`` where actual continuous-auction sessions
    matter.
    """
    now = dt or market_now()
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
    now = dt or market_now()
    if not is_trading_day(now.date()):
        return False
    return now.time() >= _MORNING_START


def previous_trading_day(day: date | None = None) -> date:
    """Return the most recent trading day on or before *day*.

    Walks backwards day-by-day; acceptable because the search
    distance is never more than ~10 calendar days (longest
    holiday break).
    """
    cursor = day or market_now().date()
    # Walk back at most 20 calendar days.
    for _ in range(20):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    # Fallback — should never be reached for realistic inputs.
    return cursor


def next_trading_day(day: date | None = None) -> date:
    """Return the earliest trading day on or after *day*."""
    cursor = day or market_now().date()
    for _ in range(20):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def calendar_data_status() -> dict:
    """chinese_calendar 数据新鲜度（P2-30）。

    库数据超界的年份会退化为 weekday-only 判定（法定假日被当成交易日，
    还会触发启动补偿的无效补跑）——用当年 12/31 与次年首个工作日探测，
    任一 NotImplementedError 即判过期，页面与部署文档同步提示升级。
    """
    now = market_now()
    stale_years: list[int] = []
    # 当年超库 → 过期（假日已被误判为交易日）
    try:
        is_workday(date(now.year, 12, 31))
    except NotImplementedError:
        stale_years.append(now.year)
    # 12 月进入升级窗口：次年数据未发布则提示升级（其余月份不误报——
    # 次年度安排通常 12 月才公布）
    if now.month >= 12 and not stale_years:
        try:
            is_workday(date(now.year + 1, 1, 4))
        except NotImplementedError:
            stale_years.append(now.year + 1)
    return {
        "stale": bool(stale_years),
        "stale_years": stale_years,
        "message": (
            "交易日历数据过期：请执行 pip install --upgrade chinese_calendar 后重启服务"
            if stale_years
            else ""
        ),
    }
