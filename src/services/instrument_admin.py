"""Instrument admin helpers shared by the instruments router and the
instrument job managers (service layer).
"""

from __future__ import annotations

from datetime import date, datetime

import logging

import pandas as pd

from core.display import load_instrument_name_map
from core.benchmarks import benchmark_instruments
from core.symbols import normalize_symbol, symbol_suffix, symbol_to_code
from data.storage.db import get_db

logger = logging.getLogger(__name__)

def _to_date(text: str, fallback: date) -> date:
    raw = str(text or "").strip()
    if raw == "":
        return fallback
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _symbol_to_code(symbol: str) -> str:
    return symbol_to_code(symbol)


def _symbol_suffix(symbol: str) -> str:
    return symbol_suffix(symbol)


def _normalize_symbol(raw_symbol: str) -> str:
    return normalize_symbol(raw_symbol)


def _date_span(df: pd.DataFrame) -> tuple[str | None, str | None]:
    if df.empty or "time" not in df.columns:
        return None, None
    series = pd.to_datetime(df["time"], errors="coerce").dropna()
    if series.empty:
        return None, None
    return series.min().date().isoformat(), series.max().date().isoformat()


def _config_name_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in get_db().list_instrument_metadata():
        symbol = str(item.get("symbol", "")).strip().upper()
        if symbol == "":
            continue
        out[symbol] = str(item.get("name", "") or "").strip()
    for item in benchmark_instruments():
        symbol = str(item.get("symbol", "")).strip().upper()
        if symbol:
            out.setdefault(symbol, str(item.get("name", "") or "").strip())
    return out


def _config_items() -> list[dict]:
    return [dict(item) for item in get_db().list_instrument_metadata()]


def _known_managed_symbols() -> set[str]:
    symbols: set[str] = set()
    for item in _config_items():
        if isinstance(item, dict):
            symbol = _normalize_symbol(item.get("symbol", ""))
            if symbol:
                symbols.add(symbol)
    for item in benchmark_instruments():
        symbol = _normalize_symbol(item.get("symbol", ""))
        if symbol:
            symbols.add(symbol)

    db = get_db()
    symbols.update(str(item.get("symbol") or "").strip().upper() for item in db.list_instrument_metadata())
    symbols.update(str(symbol or "").strip().upper() for symbol in db.list_market_symbols())
    return {symbol for symbol in symbols if symbol}


def _category_path_from_parts(l1: str, l2: str = "", l3: str = "") -> str:
    return "-".join(part for part in [l1, l2, l3] if str(part or "").strip())


def _category_priority_map() -> dict[str, int | None]:
    try:
        rows = get_db().list_instrument_categories()
    except RuntimeError as exc:
        logger.warning("Instrument categories unavailable: %s", exc)
        rows = []
    return {
        str(row.get("path") or "").strip(): row.get("priority")
        for row in rows
        if str(row.get("path") or "").strip()
    }


def category_priorities(
    l1: str,
    l2: str,
    l3: str,
    priorities: dict[str, int | None] | None = None,
) -> tuple[int | None, int | None, int | None]:
    """(l1, l2, l3) → (priority_l1, priority_l2, priority_l3)。

    公共入口：新增标的、待分类回补、存量迁移、ETF 重仓导入共用，
    不要各自平行实现（方案 §4.4）。
    """
    priorities = priorities if priorities is not None else _category_priority_map()
    return (
        priorities.get(_category_path_from_parts(l1)),
        priorities.get(_category_path_from_parts(l1, l2)),
        priorities.get(_category_path_from_parts(l1, l2, l3)),
    )


def _next_sort_order(config_items: list[dict] | None = None) -> int:
    values: list[int] = []
    for item in config_items if config_items is not None else _config_items():
        if isinstance(item, dict):
            try:
                values.append(int(item.get("sort_order") or 0))
            except (TypeError, ValueError):
                pass
    try:
        for item in get_db().list_instrument_metadata():
            try:
                values.append(int(item.get("sort_order") or 0))
            except (TypeError, ValueError):
                pass
    except RuntimeError as exc:
        logger.warning("Instrument metadata unavailable while computing sort order: %s", exc)
    return max(values or [0]) + 1


def _build_new_instrument_record(item: dict) -> dict:
    symbol = _normalize_symbol(item.get("symbol", ""))
    name = str(item.get("name") or "").strip()
    l1 = str(item.get("category_l1") or "").strip()
    l2 = str(item.get("category_l2") or "").strip()
    l3 = str(item.get("category_l3") or "").strip()
    if not symbol:
        raise ValueError("标的无效")
    if not name:
        raise ValueError("标的名称为空，请先查询名称")
    if not (l1 and l2 and l3):
        raise ValueError("一二三级类目均必选")

    p1, p2, p3 = category_priorities(l1, l2, l3)
    asset_type = "stock" if l1 == "股票" else "etf"
    return {
        "symbol": symbol,
        "name": name,
        "enabled": True,
        "risk_budget_pct": 0.01,
        "stop_atr_mul": 1.5,
        "asset_type": asset_type,
        "category_l1": l1,
        "category_l2": l2,
        "category_l3": l3,
        "factor_tags": [],
        "region_tag": "",
        "priority_l1": p1,
        "priority_l2": p2,
        "priority_l3": p3,
        "sort_order": _next_sort_order(),
        "source": "manual_add",
    }


def _append_instrument_config(record: dict) -> int:
    """Insert a new managed instrument into the metadata table (dup-rejecting)."""
    db = get_db()
    symbol = _normalize_symbol(record.get("symbol", ""))
    if db.get_instrument_metadata(symbol) is not None:
        raise ValueError(f"{symbol} 已在标的配置中")

    config_record = {
        "symbol": symbol,
        "name": str(record.get("name") or "").strip(),
        "enabled": bool(record.get("enabled", True)),
        "risk_budget_pct": record.get("risk_budget_pct", 0.01),
        "stop_atr_mul": record.get("stop_atr_mul", 1.5),
        "asset_type": str(record.get("asset_type") or "etf"),
        "category_l1": str(record.get("category_l1") or "").strip(),
        "category_l2": str(record.get("category_l2") or "").strip(),
        "category_l3": str(record.get("category_l3") or "").strip(),
        "factor_tags": record.get("factor_tags") or [],
        "region_tag": str(record.get("region_tag") or ""),
        "priority_l1": record.get("priority_l1"),
        "priority_l2": record.get("priority_l2"),
        "priority_l3": record.get("priority_l3"),
        "sort_order": record.get("sort_order"),
        "source": str(record.get("source") or ""),
    }
    return db.save_instrument_metadata([config_record])


def add_constituent_stock(
    symbol: str,
    name: str,
    known_symbols: set[str] | None = None,
) -> dict:
    """单只 ETF 重仓股入池：resolve_category 自动归类 + 写元数据（不写行情）。

    页面导入 Job 与批量导入脚本共用的唯一入口，避免平行实现。
    返回 {symbol, name, status: added|skipped|failed, ...}；重复标的安全跳过。
    """
    from services.stock_industry import resolve_category  # 延迟导入避免环依赖

    normalized = _normalize_symbol(symbol)
    name = str(name or "").strip() or normalized
    if not normalized:
        return {"symbol": "", "name": name, "status": "failed", "error": "标的代码无效"}
    known = known_symbols if known_symbols is not None else _known_managed_symbols()
    if normalized in known:
        return {"symbol": normalized, "name": name, "status": "skipped", "reason": "already_managed"}

    resolved = resolve_category(normalized)
    record = _build_new_instrument_record(
        {
            "symbol": normalized,
            "name": name,
            "category_l1": resolved["category_l1"],
            "category_l2": resolved["category_l2"],
            "category_l3": resolved["category_l3"],
        }
    )
    record["source"] = "etf_constituent"
    try:
        _append_instrument_config(record)
    except ValueError as exc:
        if "已在标的配置中" in str(exc):  # 并发重复（脚本与页面任务同时跑）
            return {"symbol": normalized, "name": name, "status": "skipped", "reason": "already_managed"}
        return {"symbol": normalized, "name": name, "status": "failed", "error": str(exc)[:200]}
    return {
        "symbol": normalized,
        "name": name,
        "status": "added",
        "category": f"{resolved['category_l2']}-{resolved['category_l3']}",
        "hit": bool(resolved["hit"]),
    }


