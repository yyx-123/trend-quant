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
            # R1-P3-4：只为取上一根 bar 的 volume——此前 load_market_data
            # 拉全量历史（874 标的 × 10 年日K），每日 14:00 白耗 IO；
            # 改为最近 10 个自然日窗口查询（长假期后仍有上一交易日 bar）
            prev_vol = 0.0
            try:
                import pandas as _pd

                win_start = (_pd.Timestamp(as_of) - _pd.Timedelta(days=10)).date()
                prev = db.load_market_data_window_many(
                    [symbol], win_start, None, price_mode="raw", period="1d",
                )
                frames = prev.get(symbol) if isinstance(prev, dict) else None
                if frames is not None and len(frames):
                    rows = frames.tail(1) if hasattr(frames, "tail") else frames
                    vol = rows["volume"].iloc[-1] if hasattr(rows, "iloc") else rows[-1]["volume"]
                    prev_vol = float(vol)
            except (TypeError, ValueError, IndexError, KeyError, AttributeError):
                prev_vol = 0.0
            try:
                out[symbol] = build_synthetic_bar(quote, prev_vol)
            except Exception:
                logger.warning("synthetic bar failed for %s", symbol, exc_info=True)
                continue
        return out

    return _overlay
