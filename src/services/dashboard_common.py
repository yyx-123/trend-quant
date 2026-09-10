"""看板共用件（P1-14）：EOD 看板（services/dashboard）与盘中看板
（data/intraday_service）原本逐字复制的辅助函数，单一来源。

两处实现的数值口径必须永远一致——``tests/unit/test_intraday_trend_consistency.py``
（cached vs 全量盘中双实现一致性）是这条约束的守门员。
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from core.numfmt import number_or_none

# 走势图/日期窗口（EOD 与盘中看板一致）：约两个月的交易日数（6 周）。
DISPLAY_DAYS = 42

# 逐日强度的「最新一个点」专用排名桶：与「强度」列同口径（各标的最新可得值
# 一起排名，不受个别标的缺当日数据影响）。哨兵值不会与 ISO 日期键冲突。
_LATEST_BUCKET = "__latest__"


def ma5(values: list[float | None]) -> list[float | None]:
    series = pd.Series(values, dtype="float64").rolling(5, min_periods=5).mean()
    return [number_or_none(value) for value in series]


def strength(values: list[float], value: float | None) -> int | None:
    if value is None or not values:
        return None
    return round(sum(score <= value for score in values) * 100 / len(values))


def priority(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 999999


def key_tuple(key: object) -> tuple[object, ...]:
    return key if isinstance(key, tuple) else (key,)


def macd_counts(instruments: list[dict]) -> dict:
    """金叉/死叉家数：类目行的 MACD 相位聚合口径（成员相位计数）。"""
    golden = sum(1 for item in instruments if item.get("macd_phase") == "golden")
    dead = sum(1 for item in instruments if item.get("macd_phase") == "dead")
    return {"macd_golden_count": golden, "macd_dead_count": dead}


def assign_strength(items: list[dict], scope_columns: tuple[str, ...]) -> None:
    """同级强度百分位：trend_ma5 在 scope 内的 percentile（0-100）。"""
    values_by_scope: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for item in items:
        value = number_or_none(item.get("trend_ma5"))
        if value is not None:
            values_by_scope[tuple(str(item[column]) for column in scope_columns)].append(value)
    for item in items:
        scope = tuple(str(item[column]) for column in scope_columns)
        item["strength"] = strength(values_by_scope[scope], number_or_none(item.get("trend_ma5")))


def assign_strength_history(items: list[dict], scope_columns: tuple[str, ...]) -> None:
    """逐日强度百分位：把 ``strength_history`` 按 ``trend_dates`` 逐位挂到各 item。

    ``assign_strength`` 只算最新一天；这里对窗口内每个交易日各算一次横截面
    百分位（口径完全相同：同一 scope 内当日 MA5 的 ``<=`` 计数占比），供趋势
    mini 图悬停提示展示逐日强度。取值来自 item 自身的 ``trend_history``（MA5
    序列），与图上画出的主线、悬停展示的 MA5 永远是同一份数据。

    窗口最新一个点走 ``_LATEST_BUCKET``：与「强度」列同口径（按各标的**最新
    可得值**排名），而不是严格的当日成员。两者只在部分标的缺当日数据时才有
    差别（停牌/盘中报价失败），但此时若不对齐，悬停最新点会与行内「强度」列
    差 1~2 个百分点。
    """
    buckets: dict[tuple[str, tuple[str, ...]], dict[int, float]] = defaultdict(dict)
    for index, item in enumerate(items):
        scope = tuple(str(item[column]) for column in scope_columns)
        history = item.get("trend_history") or []
        dates = item.get("trend_dates") or []
        for offset, (date, value) in enumerate(zip(dates, history)):
            number = number_or_none(value)
            if number is not None:
                day = _LATEST_BUCKET if offset == len(history) - 1 else str(date)[:10]
                buckets[(day, scope)][index] = number
    percentiles: dict[tuple[int, str], int] = {}
    for (day, _scope), members in buckets.items():
        series = pd.Series(members)
        # rank(method="max") 即「<= 本值的成员数」，与 strength() 同一公式；
        # 逐值计数会让全市场 × 42 日的横截面退化成 O(n²)，故用排名实现。
        ranks = series.rank(method="max") / len(series) * 100
        for index, value in ranks.round().astype(int).items():
            percentiles[(index, day)] = int(value)
    for index, item in enumerate(items):
        dates = item.get("trend_dates") or []
        item["strength_history"] = [
            percentiles.get((index, _LATEST_BUCKET if offset == len(dates) - 1 else str(date)[:10]))
            for offset, date in enumerate(dates)
        ]
