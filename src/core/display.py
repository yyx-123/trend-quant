"""Display helpers for instrument names — pure presentation logic.

Moved from app/instrument_display to the core layer so that services and
the MCP adapter can use them without importing the HTTP layer.
"""

from __future__ import annotations

import re

from core.symbols import symbol_to_code as _symbol_to_code

ETF_SUFFIX_RE = re.compile(r"\s*ETF\s*$", flags=re.IGNORECASE)


def strip_etf_suffix(name: str | None) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return ETF_SUFFIX_RE.sub("", text).strip()


def symbol_to_code(symbol: str | None) -> str:
    """510300.SS -> 510300（委托 core.symbols 的同名实现，P1-14 去重）。"""
    return _symbol_to_code(symbol)


def format_symbol_display(symbol: str | None, name: str | None = None) -> str:
    code = symbol_to_code(symbol)
    cleaned_name = strip_etf_suffix(name)
    return cleaned_name or code


def load_instrument_name_map() -> dict[str, str]:
    """Symbol -> display name map (ETF suffix stripped), from the metadata table."""
    import sqlite3

    from data.storage.db import get_db

    try:
        items = get_db().list_instrument_metadata()
    except (RuntimeError, sqlite3.Error):
        return {}  # database unavailable (e.g. bare unit-test context)
    out: dict[str, str] = {}
    for item in items:
        symbol = str(item.get("symbol", "")).strip().upper()
        if symbol == "":
            continue
        out[symbol] = strip_etf_suffix(str(item.get("name", "") or ""))
    return out


def build_symbol_display(symbol: str | None, name_map: dict[str, str] | None = None) -> str:
    normalized = str(symbol or "").strip().upper()
    map_name = ""
    if name_map is not None:
        map_name = str(name_map.get(normalized, "") or "")
    return format_symbol_display(normalized, map_name)


def filter_fully_classified(symbols: list[str], metadata_map: dict[str, dict]) -> list[str]:
    """Keep only symbols with a complete L1/L2/L3 category classification.

    Used by the intraday dashboard snapshot job (only fully classified
    instruments enter the dashboard).
    """
    return [
        s
        for s in symbols
        if s in metadata_map
        and str(metadata_map[s].get("category_l1", "")).strip()
        and str(metadata_map[s].get("category_l2", "")).strip()
        and str(metadata_map[s].get("category_l3", "")).strip()
    ]


def category_path(meta: dict | None) -> str:
    """三级类目路径「L1-L2-L3」（P1-14：原 db/instruments/market_view/
    trend_mcp 四份 _category_path 复制的单一来源）。"""
    if not meta:
        return ""
    parts = [
        str(meta.get("category_l1") or "").strip(),
        str(meta.get("category_l2") or "").strip(),
        str(meta.get("category_l3") or "").strip(),
    ]
    return "-".join(part for part in parts if part)


def category_path_from_parts(l1: str, l2: str = "", l3: str = "") -> str:
    return "-".join(part for part in [l1, l2, l3] if str(part or "").strip())
