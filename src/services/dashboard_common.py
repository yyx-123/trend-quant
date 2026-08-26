"""看板共用件（P1-14）：EOD 看板（services/dashboard）与盘中看板
（data/intraday_service）原本逐字复制的辅助函数，单一来源。

两处实现的数值口径必须永远一致——``tests/unit/test_intraday_trend_consistency.py``
（cached vs 全量盘中双实现一致性）是这条约束的守门员。
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from core.numfmt import number_or_none

# 走势图/日期窗口（EOD 与盘中看板一致）。
DISPLAY_DAYS = 61


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
