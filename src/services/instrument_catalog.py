"""标的目录查询 — MCP ``list_instruments`` 工具的唯一实现。

从 instrument_metadata 读取全量目录，按分类/关键词/启用状态筛选，
数据范围（rows/start/end）经 ``get_market_data_summary_many`` 单连接
批量补齐（避免逐标的主键查询）。
"""

from __future__ import annotations

import sqlite3

from audit.app_logger import get_logger
from data.storage.db import get_db

logger = get_logger(__name__)


def search_instruments(
    category: str = "",
    keyword: str = "",
    enabled_only: bool = True,
    db=None,
) -> dict:
    """列出可用标的，支持按三级分类和代码/名称关键词筛选。"""
    db = db or get_db()
    try:
        instruments = [dict(item) for item in db.list_instrument_metadata()]
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Instrument metadata unavailable: %s", exc)
        instruments = []

    category_kw = category.strip().lower()
    keyword_kw = keyword.strip().lower()

    kept: list[dict] = []
    for item in instruments:
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        if enabled_only and not item.get("enabled", True):
            continue

        cat_l1 = str(item.get("category_l1") or "")
        cat_l2 = str(item.get("category_l2") or "")
        cat_l3 = str(item.get("category_l3") or "")
        name = str(item.get("name") or "")

        if category_kw and not (
            category_kw in cat_l1.lower()
            or category_kw in cat_l2.lower()
            or category_kw in cat_l3.lower()
        ):
            continue
        if keyword_kw and not (keyword_kw in symbol.lower() or keyword_kw in name.lower()):
            continue

        kept.append(
            {
                "symbol": symbol,
                "name": name,
                "category_l1": cat_l1,
                "category_l2": cat_l2,
                "category_l3": cat_l3,
                "enabled": bool(item.get("enabled", True)),
            }
        )

    summaries = db.get_market_data_summary_many([item["symbol"] for item in kept])
    for item in kept:
        summary = summaries.get(item["symbol"], {})
        item["data_rows"] = int(summary.get("rows") or 0)
        item["data_start"] = str(summary["start"]) if summary.get("start") else None
        item["data_end"] = str(summary["end"]) if summary.get("end") else None

    return {"ok": True, "count": len(kept), "instruments": kept}
