"""Symbol normalization — the single implementation project-wide.

Canonical rules:
- Bare 6-digit codes: leading 5/6 -> Shanghai (.SS), otherwise Shenzhen (.SZ)
- Already-suffixed symbols are uppercased and passed through, with the
  legacy ``.SH`` suffix normalized to ``.SS``

The frontend JS mirror in instruments.html is an input-preview only;
the server is authoritative.
"""

from __future__ import annotations


def normalize_symbol(raw_symbol: str) -> str:
    text = str(raw_symbol or "").strip().upper()
    if text == "":
        return ""
    if "." in text:
        code, suffix = text.split(".", 1)
        if suffix == "SH":
            suffix = "SS"
        return f"{code}.{suffix}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return text
    suffix = ".SS" if digits.startswith(("5", "6")) else ".SZ"
    return f"{digits}{suffix}"


def symbol_to_code(symbol: str) -> str:
    """510300.SS -> 510300 (bare 6-digit code)."""
    text = str(symbol or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def symbol_suffix(symbol: str) -> str:
    """510300.SS -> SS (empty string when unsuffixed)."""
    text = str(symbol or "").strip().upper()
    if "." in text:
        return text.split(".", 1)[1]
    return ""


def to_vendor_symbol(symbol: str) -> str:
    """项目代码 → vendor（tickflow/tushare）格式：.SS → .SH，其余不变。

    P1-14：原 provider_tickflow._to_tickflow_symbol 与
    stock_industry.project_symbol_to_tushare 两份复制的单一来源。
    """
    text = str(symbol or "").strip().upper()
    if text.endswith(".SS"):
        return f"{text[:-3]}.SH"
    return text


def from_vendor_symbol(symbol: str) -> str:
    """vendor（tickflow/tushare）代码 → 项目格式：.SH → .SS，其余不变。"""
    text = str(symbol or "").strip().upper()
    if text.endswith(".SH"):
        return f"{text[:-3]}.SS"
    return text
