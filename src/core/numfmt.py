"""数值规整公共件（P1-14）：原 dashboard/intraday_service/market_indicators/
market_view 四处 ``_number``/``_num`` 复制的单一来源。

两种口径历史成因不同，保留为两个显式命名的函数：
- ``number_or_none``：float 化 + 非有限值（NaN/inf）归 None（看板聚合用）；
- ``number6_or_none``：float 化 + NaN 归 None + 保留 6 位小数（API 输出用）。
"""

from __future__ import annotations

from math import isfinite

import pandas as pd


def number_or_none(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def number6_or_none(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pd.isna(n):
        return None
    return round(n, 6)
