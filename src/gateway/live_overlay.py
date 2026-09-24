"""实盘盘中合成 bar 提供者（详设 §3.1 live 模式：复用 intraday_service 的
合成逻辑）。放在 L1.5 包内——L1.5 调 L1 是合法的；L3（live.py）不 import
L1 数据服务（评审 A-P1-2 分层铁律）。
"""

from __future__ import annotations

from audit.app_logger import get_logger

logger = get_logger(__name__)


def default_live_overlay(db):
    """TickFlow 实时报价 → 盘中合成 bar（内存合成不落库）。

    供 live 模式面板在 as_of 当日并入一根 provisional bar。
    """

    def _overlay(symbols, as_of):
        from data.intraday_service import build_synthetic_bar
        from data.service import get_data_service

        service = get_data_service()
        quotes = service.fetch_latest_quotes(list(symbols))
        out = {}
        for symbol, quote in (quotes or {}).items():
            if not quote:
                continue
            prev = db.load_market_data(symbol)
            prev_vol = 0.0
            if prev is not None and not prev.empty:
                try:
                    prev_vol = float(prev["volume"].iloc[-1])
                except (TypeError, ValueError, IndexError):
                    prev_vol = 0.0
            try:
                out[symbol] = build_synthetic_bar(quote, prev_vol)
            except Exception:
                logger.warning("synthetic bar failed for %s", symbol, exc_info=True)
                continue
        return out

    return _overlay
