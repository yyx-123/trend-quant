"""K线 DataFrame 的通用小工具（P1-14）：原 data/service 与
services/instrument_admin 两处逐字相同的 ``_date_span`` 的单一来源。"""

from __future__ import annotations

import pandas as pd


def date_span(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """K线 DataFrame 的（首日, 末日）ISO 日期；空表/无时间列返回 (None, None)。"""
    if df.empty or "time" not in df.columns:
        return None, None
    series = pd.to_datetime(df["time"], errors="coerce").dropna()
    if series.empty:
        return None, None
    return series.min().date().isoformat(), series.max().date().isoformat()
