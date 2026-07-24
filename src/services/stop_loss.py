"""止损价计算 — 硬止损 / 吊灯止损的单一实现来源。

消费方：
- MCP 工具 ``trend_mcp/server.py`` 的 ``calc_stop_loss``
- 手工交易聚合服务 ``services/manual_trade.py``

止损公式与回测引擎一致（见 ``rule_backtest/state_values.py``）：
- 硬止损 = 买入价 − ATR(20, 买入日) × hard_stop_atr_mul（默认 1.5，
  支持 per-instrument ``stop_atr_mul`` 覆盖）
- 吊灯止损 = 买入以来最高价 − ATR(20, 最新) × chandelier_stop_atr_mul（默认 2.5）

盘中实时叠加（``intraday=True`` 且处于交易时段 9:30-15:00，含午间休盘）：
- 用实时报价合成当日K线，计入「买入以来最高价」「最新价」「止损触发判断」，
  因此吊灯止损价在盘中会随新高实时上移；
- ATR 沿用历史完整K线的值（与实时看板 ``compute_intraday_trend_score``
  同一口径），避免当日不完整K线污染 ATR；
- 非交易时段或报价获取失败时静默回退为纯日K（EOD）结果。
"""

from __future__ import annotations

import logging
import sqlite3

import pandas as pd

from core.calendar import is_realtime_available
from core.strategy_config import get_strategy_config
from core.symbols import normalize_symbol
from core.trend import safe_float
from data.indicator_store import get_series
from data.intraday_service import build_synthetic_bar
from data.service import DataService
from data.storage.db import get_db

logger = logging.getLogger(__name__)

#: 哨兵值：调用方未提供预取的盘中合成K线 —— 按默认路径实时拉取。
#: 显式传 None 表示「已确认无盘中数据，直接用 EOD」（交易记录列表的同 symbol 去重用）。
UNSET_INTRADAY_BAR: object = object()


class StopLossError(ValueError):
    """止损计算中的业务错误（无效输入 / 数据不足）。"""


def _load_instrument_metadata(db) -> list[dict]:
    try:
        return [dict(item) for item in db.list_instrument_metadata()]
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Instrument metadata unavailable: %s", exc)
        return []


def _fetch_intraday_bar(symbol: str, df: pd.DataFrame) -> dict | None:
    """交易时段内（含午间休盘）用实时报价合成当日K线；否则/失败返回 None。

    与 ``symbol_detail`` 的 intraday overlay 同一路径：
    ``is_realtime_available`` 门控 + ``DataService.fetch_latest_quote``
    + ``build_synthetic_bar``，任何失败都静默回退 EOD。
    """
    if not is_realtime_available():
        return None
    try:
        quote = DataService().fetch_latest_quote(symbol)
        if not quote or quote.get("price") is None:
            return None
        volumes = pd.to_numeric(df["volume"], errors="coerce") if len(df) else pd.Series(dtype=float)
        prev_vol = safe_float(volumes.iloc[-1], 0.0) if len(volumes) else 0.0
        return build_synthetic_bar(quote, prev_vol)
    except Exception as exc:
        logger.warning("Intraday quote failed for %s; falling back to EOD: %s", symbol, exc)
        return None


def fetch_intraday_bar(symbol: str, df: pd.DataFrame) -> dict | None:
    """``_fetch_intraday_bar`` 的公开包装：交易记录列表按 symbol 预取去重用。"""
    return _fetch_intraday_bar(symbol, df)


def compute_stop_loss(
    symbol: str,
    buy_date: str,
    buy_price: float,
    db=None,
    intraday: bool = True,
    end_date: str | None = None,
    intraday_bar: dict | None | object = UNSET_INTRADAY_BAR,
) -> dict:
    """计算给定买入的硬止损价和吊灯止损价。

    硬止损公式: 买入价 − 买入当日 ATR(20) × hard_stop_atr_mul (默认 1.5)。
    注意以买入价而非买入当日收盘价为基准 —— 手工输入的买入价通常不是收盘价。
    吊灯止损公式: 买入以来最高价 − 最新 ATR(20) × chandelier_stop_atr_mul (默认 2.5)。

    ``intraday=True``（默认）时，交易时段内会把实时报价合成的当日K线计入
    最高价 / 最新价 / 止损触发判断；ATR 仍为历史完整K线口径（见模块 docstring）。
    ``intraday_bar`` 显式传入（含 None）时跳过实时拉取，直接使用该值。

    ``end_date`` 用于已清仓交易：数据与 ATR 均截断到该日（含），
    "最新价 / 买入以来最高价" 都按截止日口径，且强制关闭 intraday。

    Raises:
        StopLossError: 标的无效、无数据或 ATR 异常。
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise StopLossError("无效的标的代码")
    try:
        buy_ts = pd.Timestamp(buy_date)
    except (ValueError, TypeError) as exc:
        raise StopLossError(f"无效的买入日期: {buy_date}") from exc
    if buy_price <= 0:
        raise StopLossError("买入价格必须大于 0")

    end_ts: pd.Timestamp | None = None
    if end_date is not None:
        try:
            end_ts = pd.Timestamp(end_date)
        except (ValueError, TypeError) as exc:
            raise StopLossError(f"无效的截止日期: {end_date}") from exc
        if end_ts < buy_ts:
            raise StopLossError(f"截止日期 {end_date} 早于买入日期 {buy_date}")
        intraday = False  # 历史截断口径，不叠加盘中

    db = db or get_db()
    df = db.load_market_data(symbol)
    if df.empty:
        raise StopLossError(f"未找到 {symbol} 的数据")

    strategy_cfg = get_strategy_config()
    hard_stop_mul = float(strategy_cfg.get("hard_stop_atr_mul_default", 1.5))
    chandelier_mul = float(strategy_cfg.get("chandelier_stop_atr_mul", 2.5))

    # Per-instrument stop_atr_mul override (DB rows may carry NULL)
    for item in _load_instrument_metadata(db):
        if str(item.get("symbol", "")).strip().upper() == symbol:
            if item.get("stop_atr_mul") is not None:
                hard_stop_mul = float(item["stop_atr_mul"])
            break

    # ATR from the precomputed cache (single source, D11); the store falls
    # back to a live full-history compute when the cache is stale/missing.
    atr_series = get_series(symbol, "atr", db=db)
    if end_ts is not None:
        atr_series = atr_series[atr_series.index <= end_ts]
    if atr_series.empty:
        raise StopLossError("数据不足，无法计算 ATR")

    current_atr = safe_float(atr_series.iloc[-1], 0.0)
    if current_atr <= 0:
        raise StopLossError("ATR 值为 0，数据异常")

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    if end_ts is not None:
        df = df[df["time"] <= end_ts]
        if df.empty:
            raise StopLossError(f"{symbol} 在 {end_date}（含）之前无数据")

    # 盘中实时叠加：合成当日K线计入最高价/最新价。
    # ATR 刻意不叠加当日不完整K线（与实时看板同一口径），故无需重算。
    if intraday:
        synth = (
            _fetch_intraday_bar(symbol, df)
            if intraday_bar is UNSET_INTRADAY_BAR
            else intraday_bar
        )
    else:
        synth = None
    if synth is not None:
        today = pd.Timestamp(synth["time"]).normalize()
        df = df[df["time"] < today]  # 防御：剔除可能已存在的当日行
        df = pd.concat([df, pd.DataFrame([synth])], ignore_index=True)

    # 买入价合理性校验：必须落在买入日当根K线的最高/最低价之间。
    # 买入日为非交易日（无当根K线）时跳过 —— 历史行为允许非交易日买入。
    day_bars = df[df["time"].dt.normalize() == buy_ts]
    if not day_bars.empty:
        day_low = safe_float(pd.to_numeric(day_bars["low"], errors="coerce").iloc[0], 0.0)
        day_high = safe_float(pd.to_numeric(day_bars["high"], errors="coerce").iloc[0], 0.0)
        eps = max(1e-4, abs(day_high) * 1e-6)
        if day_low > 0 and day_high > 0 and not (day_low - eps <= buy_price <= day_high + eps):
            raise StopLossError(
                f"买入价格 {buy_price} 超出 {buy_date} 当日价格区间 "
                f"[{round(day_low, 4)}, {round(day_high, 4)}]"
            )

    # ATR at buy date (look back up to and including buy_date)
    atr_at_buy = current_atr
    subset = atr_series[atr_series.index <= buy_ts]
    if not subset.empty and pd.notna(subset.iloc[-1]):
        atr_at_buy = safe_float(subset.iloc[-1], current_atr)

    # Highest price since buy date (inclusive)
    highs = pd.to_numeric(df["high"], errors="coerce")
    latest_price = safe_float(pd.to_numeric(df["close"], errors="coerce").iloc[-1], 0.0)
    highest_since_buy = latest_price
    mask_since = df["time"] >= buy_ts
    if mask_since.any():
        since_highs = highs[mask_since]
        if not since_highs.empty and since_highs.notna().any():
            highest_since_buy = safe_float(since_highs.max(), latest_price)

    # Calculate stop prices
    hard_stop_price = round(buy_price - hard_stop_mul * atr_at_buy, 4)
    chandelier_stop_price = round(highest_since_buy - chandelier_mul * current_atr, 4)

    hard_stop_pct = round((hard_stop_price / buy_price - 1) * 100, 2)
    chandelier_pct = (
        round((chandelier_stop_price / highest_since_buy - 1) * 100, 2)
        if highest_since_buy > 0
        else 0.0
    )

    payload = {
        "symbol": symbol,
        "buy_price": buy_price,
        "buy_date": buy_date,
        "hard_stop_price": hard_stop_price,
        "hard_stop_pct": hard_stop_pct,
        "hard_stop_atr_mul": hard_stop_mul,
        "chandelier_stop_price": chandelier_stop_price,
        "chandelier_stop_pct_from_high": chandelier_pct,
        "chandelier_stop_atr_mul": chandelier_mul,
        "atr_at_buy": round(atr_at_buy, 4),
        "current_atr": round(current_atr, 4),
        "highest_since_buy": round(highest_since_buy, 4),
        "latest_price": round(latest_price, 4),
        "is_intraday": synth is not None,
    }
    if synth is not None:
        payload["intraday_ts"] = pd.Timestamp(synth["time"]).isoformat()
        payload["intraday_bar"] = {
            "date": str(pd.Timestamp(synth["time"]).date()),
            "open": round(float(synth["open"]), 4),
            "high": round(float(synth["high"]), 4),
            "low": round(float(synth["low"]), 4),
            "close": round(float(synth["close"]), 4),
        }
    return payload
