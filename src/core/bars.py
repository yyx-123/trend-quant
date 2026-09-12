"""K线 DataFrame 的通用小工具（P1-14）：原 data/service 与
services/instrument_admin 两处逐字相同的 ``_date_span`` 的单一来源。

2026-09-12 增补：K 线周期（日/周/月）口径 —— 周期规范化与「未收盘 bar
不落库」过滤。周/月K 的周期语义只定义在这里，db/provider/service 均调用
本模块，避免各处各写一套（与 core.indicators 的单一实现约定一致）。
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pandas as pd

from core.calendar import is_trading_day, market_now

# 内部规范周期值即 vendor（tickflow）period 取值：1d / 1w / 1M。
# 注意大小写：'1M' 是月线，小写 '1m' 是分钟线 —— 二者绝不互相兼容，
# 别名表刻意不收录 'm'，让分钟线误传在入口就报错而不是静默落成月K。
PERIOD_DAILY = "1d"
PERIOD_WEEKLY = "1w"
PERIOD_MONTHLY = "1M"
SUPPORTED_PERIODS = (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY)

_PERIOD_ALIASES = {
    "1d": PERIOD_DAILY,
    "d": PERIOD_DAILY,
    "D": PERIOD_DAILY,
    "day": PERIOD_DAILY,
    "daily": PERIOD_DAILY,
    "1w": PERIOD_WEEKLY,
    "w": PERIOD_WEEKLY,
    "W": PERIOD_WEEKLY,
    "week": PERIOD_WEEKLY,
    "weekly": PERIOD_WEEKLY,
    "1M": PERIOD_MONTHLY,
    "M": PERIOD_MONTHLY,
    "month": PERIOD_MONTHLY,
    "monthly": PERIOD_MONTHLY,
}

# 会话收盘时刻（Beijing）：当日 bar 在收盘前一律视为未完成。
_SESSION_CLOSE = time(15, 0)


def normalize_period(period: str | None) -> str:
    """周期别名 → 规范值（1d/1w/1M）；未知值抛 ValueError。

    'm'/'1m'（分钟线）不在别名表内：tickflow 的分钟线与本项目的日/周/月
    周期存储不是一回事，误传必须显式失败而非落到月线。
    """
    value = str(period or "").strip()
    if not value:
        return PERIOD_DAILY
    canonical = _PERIOD_ALIASES.get(value)
    if canonical is None:
        raise ValueError(
            f"unsupported kline period: {period!r} (supported: {', '.join(SUPPORTED_PERIODS)})"
        )
    return canonical


def _period_key(day: date, period: str) -> tuple[int, ...]:
    """周期标识：周=ISO(年,周)，月=(年,月)。"""
    if period == PERIOD_WEEKLY:
        iso = day.isocalendar()
        return (int(iso[0]), int(iso[1]))
    return (day.year, day.month)


def _period_end(day: date, period: str) -> date:
    """该周期的日历末日：周=ISO 周日，月=月末。"""
    if period == PERIOD_WEEKLY:
        return day + timedelta(days=6 - day.weekday())
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def is_period_bar_closed(bar_day: date, period: str, *, now=None) -> bool:
    """该 bar 所属周期是否已经走完（可以落库）。

    判据只用一条：**该周期在今天的日历刻度上是否已经过去**。
    往期 bar 一律已收盘（O(1)，不查日历）；只有落在当期的 bar 才需要看
    本期最后一个交易日过没过（含收盘时刻判定）。

    这里刻意不用「bar 标注日 + 下一交易日是否跨周期」的判断：vendor 的周期
    bar 标注日是该**标的**在本周期内最后一个有成交的日子，停牌会让它落在
    周期中间（例：万科A 2015-12 月 bar 标 12-18、招行 2015-04 周 bar 标
    04-02），按相邻交易日判会把这些合法 bar 误判为「进行中」而丢弃。

    日线恒返回 True：日 bar 的落库节奏由既有日更流程决定，此处不改变行为。
    """
    canonical = normalize_period(period)
    if canonical == PERIOD_DAILY:
        return True

    if not isinstance(bar_day, date):
        return False
    moment = now or market_now()
    today = moment.date()
    if bar_day > today:
        return False

    bar_key = _period_key(bar_day, canonical)
    today_key = _period_key(today, canonical)
    if bar_key < today_key:
        return True
    if bar_key > today_key:
        return False

    # 当期 bar：本期最后一个交易日若还没到，就是进行中。
    # 本期剩余交易日（不含今天）——有则本期未走完；没有才轮到「今天是否
    # 本期最后一个交易日 + 是否已收盘」的判定。
    cursor, stop = today + timedelta(days=1), _period_end(today, canonical)
    while cursor <= stop:
        if is_trading_day(cursor):
            return False
        cursor += timedelta(days=1)
    if is_trading_day(today):
        return moment.time() >= _SESSION_CLOSE
    # 今天不是交易日、本期也无更晚的交易日 → 本期已走完（假期中）
    return True


def closed_bars(df: pd.DataFrame, period: str, *, now=None) -> pd.DataFrame:
    """丢弃周期尚未走完的 bar（周/月K 专用；日K 原样返回）。

    入参/出参均为标准 OHLCV DataFrame（含 time 列）；返回新对象，不修改入参。
    """
    canonical = normalize_period(period)
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    if canonical == PERIOD_DAILY or "time" not in df.columns:
        return df

    days = pd.to_datetime(df["time"], errors="coerce")
    keep = [
        not pd.isna(day) and is_period_bar_closed(day.date(), canonical, now=now)
        for day in days
    ]
    return df.loc[keep].reset_index(drop=True)


def date_span(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """K线 DataFrame 的（首日, 末日）ISO 日期；空表/无时间列返回 (None, None)。"""
    if df.empty or "time" not in df.columns:
        return None, None
    series = pd.to_datetime(df["time"], errors="coerce").dropna()
    if series.empty:
        return None, None
    return series.min().date().isoformat(), series.max().date().isoformat()
