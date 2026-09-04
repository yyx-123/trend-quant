"""单标的详情载荷 — 历史日K + 全套指标 + 可选盘中实时叠加。

MCP ``symbol_detail`` 工具的唯一实现；工具层只负责参数透传。与 Web
``/api/daily`` 共用同一套底层件（compute_market_indicators /
build_intraday_overlay），EMA 族指标在全量历史上计算、输出再截尾
（先截尾再算会让数值随请求窗口漂移——历史 window-truncation bug）。
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from audit.app_logger import get_logger
from core.display import category_path, format_symbol_display, load_instrument_name_map
from core.symbols import normalize_symbol
from data.intraday_service import build_intraday_overlay
from data.storage.db import get_db
from services.market_indicators import compute_market_indicators, trend_config

logger = get_logger(__name__)


def _instrument_metadata(db, symbol: str) -> dict | None:
    try:
        return db.get_instrument_metadata(symbol)
    except (RuntimeError, sqlite3.Error, AttributeError) as exc:
        logger.warning("Instrument metadata unavailable for %s: %s", symbol, exc)
        return None


def _tail(values: list, n: int) -> list:
    return values[-n:] if len(values) > n else values


def _float_list(series_like) -> list:
    return [round(float(v), 4) if pd.notna(v) else None for v in series_like]


def symbol_detail_payload(
    symbol: str,
    days: int = 60,
    rsi_period: int = 14,
    intraday: bool = False,
    db=None,
) -> dict:
    """指定标的的日K + 指标载荷；``intraday=True`` 时叠加当日合成K线。

    盘中叠加（``data.intraday_service.build_intraday_overlay``，与 Web 日K
    接口同一实现）在交易日 9:30 后生效：当日K线未落库时追加一根由实时
    报价合成的K线，并在 ``indicators.trend_intraday`` 返回盘中趋势值；
    当日K已落库 / 非交易时段 / 报价失败时静默回退为纯日K。
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        return {"ok": False, "error": "无效的标的代码"}

    db = db or get_db()
    df = db.load_market_data(symbol)
    if df.empty:
        return {"ok": False, "error": f"未找到 {symbol} 的数据，请确认代码正确且数据已入库"}

    requested = max(int(days), 1)
    rsi_period = max(2, int(rsi_period or 14))

    metadata = _instrument_metadata(db, symbol)
    name = str((metadata or {}).get("name") or "").strip() or load_instrument_name_map().get(symbol, "")

    trend_cfg = trend_config()
    indicators = compute_market_indicators(df, trend_cfg=trend_cfg, rsi_period=rsi_period)

    n = min(requested, len(df))
    full_df = df  # 盘中趋势用全量历史（与 EOD 同一把尺子），不用截尾窗口
    df = df.tail(n).copy()

    payload = {
        "ok": True,
        "symbol": symbol,
        "name": name,
        "display_name": format_symbol_display(symbol, name),
        "category": category_path(metadata),
        "category_l1": str((metadata or {}).get("category_l1") or ""),
        "category_l2": str((metadata or {}).get("category_l2") or ""),
        "category_l3": str((metadata or {}).get("category_l3") or ""),
        "meta": db.get_market_data_summary(symbol),
        "dates": [str(d.date()) for d in df["time"]],
        "candles": {
            "open": _tail(_float_list(df["open"]), n),
            "high": _tail(_float_list(df["high"]), n),
            "low": _tail(_float_list(df["low"]), n),
            "close": _tail(_float_list(df["close"]), n),
        },
        "volumes": _tail(
            [int(v) if pd.notna(v) else None for v in df.get("volume", pd.Series())], n
        ),
        "indicators": indicators,
    }
    payload["meta"]["is_intraday"] = False

    if intraday:
        overlay = build_intraday_overlay(symbol, full_df, trend_cfg)
        if overlay:
            bar = overlay["bar"]
            payload["dates"].append(overlay["date"])
            payload["candles"]["open"].append(round(float(bar["open"]), 4))
            payload["candles"]["high"].append(round(float(bar["high"]), 4))
            payload["candles"]["low"].append(round(float(bar["low"]), 4))
            payload["candles"]["close"].append(round(float(bar["close"]), 4))
            payload["volumes"].append(int(bar["volume"]))
            payload["indicators"]["trend_intraday"] = overlay["trend"]
            payload["meta"]["is_intraday"] = True
            payload["meta"]["intraday_ts"] = overlay["ts"]
            payload["meta"]["post_close"] = bool(overlay.get("post_close"))
            # dates 已含当日合成K线：meta.end 必须与载荷一致，并标注其为
            # 合成（未落库）数据，避免消费方拿 meta.end 误判数据新鲜度。
            payload["meta"]["end"] = overlay["date"]
            payload["meta"]["end_is_synthetic"] = True

    return payload
