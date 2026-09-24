"""元数据透出（详设 §2.2/§3）：日历、类目、上市日期、股票池成员。

链路固定：L1 先存 → L1.5 元数据接口透出 → 上层消费；上层永远不直连 L1。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.calendar import is_trading_day


def trading_days(start: date | str, end: date | str) -> list[date]:
    """[start, end] 内的 A 股交易日（core.calendar 单一实现）。"""
    start_day = pd.Timestamp(start).date()
    end_day = pd.Timestamp(end).date()
    days: list[date] = []
    cursor = start_day
    one = pd.Timedelta(days=1)
    while cursor <= end_day:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor = (pd.Timestamp(cursor) + one).date()
    return days


class MetadataService:
    """db 之上的只读元数据面。"""

    def __init__(self, db) -> None:
        self._db = db

    def trading_days(self, start: date | str, end: date | str) -> list[date]:
        return trading_days(start, end)

    def instruments(self, symbols: list[str] | None = None) -> dict[str, dict]:
        """instrument_metadata 透出（含类目三级、asset_type、start_date）。"""
        meta = self._db.get_instrument_metadata_map()
        if symbols is None:
            return meta
        wanted = {str(s).strip().upper() for s in symbols}
        return {s: m for s, m in meta.items() if s in wanted}

    def enabled_symbols(self, asset_type: str | None = None) -> list[str]:
        """当前 enabled 股票池成员（时点动态池是数据线二期的事）。"""
        meta = self._db.get_instrument_metadata_map()
        out = []
        for symbol, row in meta.items():
            if not row.get("enabled", 1):
                continue
            if (
                asset_type
                and asset_type != "all"
                and str(row.get("asset_type") or "").lower() != asset_type.lower()
            ):
                continue
            out.append(symbol)
        return sorted(out)

    def industry(self, symbols: list[str]) -> dict[str, dict]:
        """申万行业分类透出（stock_industry 表）。"""
        return {r["symbol"]: r for r in self._db.list_stock_industry(symbols)}
