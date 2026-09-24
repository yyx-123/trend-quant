"""止损原语（收编双实现，详设 §4.3/§4.3.1）。

职责切分：本模块提供止损**公式**的纯函数实现与 StopState 逐日维护
助手；止损的**执行语义**（盘中触及按止损价成交、跳空按开盘、跌停顺延
重试）归 matcher.py。止损公式本身经 L3 `position_risk` 槽可插拔——
新公式 = 注册新模块，引擎无需改动。

统一口径（决策 6）：
- 以**实际买入价**为基准（entry_price，不是含费 avg_cost）；
- **ATR 含当根**：入场日初始化时，成交时刻当根 bar 已知，ATR(20) 含
  当根（与现行实盘侧 services/stop_loss.py 一致，回测侧向实盘侧对齐）；
- **日内路径口径（写死）**：持仓期间逐日判定时，T 日的止损价用
  **T-1 的 highest_since_buy / T-1 截止的数据**计算，再与 T 日
  open/low 比较；禁止用"含当日最高价"更新后的止损价去判定当日最低价。
  （两者作用于不同阶段：决策 6 管入场初始化，本条管持仓期逐日判定。）

``daily_*`` 系列函数在"判定前"调用（入参为 T-1 状态 + 截至 T-1 的数据），
``post_day_*`` 在当日结算后调用（把 T 日的信息并入状态，供次日判定）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import atr as _atr_series
from core.indicators import sma as _sma
from engine.models import StopState


def atr_at(series_df: pd.DataFrame, period: int = 20) -> float:
    """给定截至某日的 OHLC 序列，返回最新 ATR（含最后一根 bar）。"""
    if series_df is None or series_df.empty:
        return 0.0
    values = _atr_series(series_df, period=period)
    if values.empty:
        return 0.0
    last = float(values.iloc[-1])
    return last if np.isfinite(last) else 0.0


# ----------------------------------------------------------------------
# 入场初始化（ATR 含当根）
# ----------------------------------------------------------------------


def init_hard_stop(*, entry_price: float, atr_including_entry: float, atr_mul: float) -> StopState:
    stop = entry_price - atr_mul * atr_including_entry if atr_including_entry > 0 else None
    return StopState(
        stop_price=stop,
        highest_since_buy=entry_price,
        atr_at_entry=atr_including_entry,
        fill_mode="intraday_stop",
    )


def init_chandelier(
    *,
    entry_price: float,
    entry_day_high: float,
    atr_including_entry: float,
    atr_mul: float,
    ratchet: bool = False,
) -> StopState:
    highest = max(entry_day_high, entry_price)
    stop = highest - atr_mul * atr_including_entry if atr_including_entry > 0 else None
    return StopState(
        stop_price=stop,
        highest_since_buy=highest,
        atr_at_entry=atr_including_entry,
        fill_mode="intraday_stop",
        module_state={"ratchet": bool(ratchet)},
    )


def init_ma_stop(*, entry_price: float, entry_day_high: float,
                 atr_including_entry: float, ma_value: float | None) -> StopState:
    """均线止损：收盘确认型（fill_mode=tail），heat 用 MA 参考线记近似。"""
    return StopState(
        stop_price=ma_value,
        highest_since_buy=max(entry_day_high, entry_price),
        atr_at_entry=atr_including_entry,
        fill_mode="tail",
        heat_approximate=True,
    )


def init_pct_trailing(*, entry_price: float, entry_day_high: float,
                      atr_including_entry: float, pct: float) -> StopState:
    highest = max(entry_day_high, entry_price)
    return StopState(
        stop_price=highest * (1.0 - pct),
        highest_since_buy=highest,
        atr_at_entry=atr_including_entry,
        fill_mode="intraday_stop",
    )


def init_donchian(*, entry_price: float, entry_day_high: float,
                  atr_including_entry: float, channel_low: float | None) -> StopState:
    return StopState(
        stop_price=channel_low,
        highest_since_buy=max(entry_day_high, entry_price),
        atr_at_entry=atr_including_entry,
        fill_mode="intraday_stop",
    )


def init_breakeven(*, entry_price: float, entry_day_high: float,
                   atr_including_entry: float, trigger_atr: float) -> StopState:
    """保本止损：浮盈 > trigger_atr × ATR 后止损上移至买入价（1-bit 激活态）。"""
    return StopState(
        stop_price=None,
        highest_since_buy=max(entry_day_high, entry_price),
        atr_at_entry=atr_including_entry,
        fill_mode="intraday_stop",
        module_state={"activated": False, "trigger_atr": float(trigger_atr)},
    )


def init_time_stop(*, entry_price: float, entry_day_high: float,
                   atr_including_entry: float, max_days: int) -> StopState:
    return StopState(
        stop_price=None,
        highest_since_buy=max(entry_day_high, entry_price),
        atr_at_entry=atr_including_entry,
        fill_mode="tail",
        module_state={"max_days": int(max_days), "held_days": 0},
    )


def init_none(*, entry_price: float, entry_day_high: float,
              atr_including_entry: float) -> StopState:
    """none：不止损（heat 记 None 并告警由报告层处理）。"""
    return StopState(
        stop_price=None,
        highest_since_buy=max(entry_day_high, entry_price),
        atr_at_entry=atr_including_entry,
        fill_mode="tail",
    )


# ----------------------------------------------------------------------
# 逐日判定（T 日判定用 T-1 状态——日内路径口径）
# ----------------------------------------------------------------------


def daily_chandelier_stop(state: StopState, *, atr_through_yesterday: float,
                          atr_mul: float) -> float | None:
    """T 日使用的吊灯止损价 = highest(T-1) − mul × ATR(T-1)。

    棘轮变体：与前一交易日止损价取 max（只上移）。
    """
    if atr_through_yesterday <= 0 or state.highest_since_buy <= 0:
        return state.stop_price
    candidate = state.highest_since_buy - atr_mul * atr_through_yesterday
    if state.module_state.get("ratchet") and state.stop_price is not None:
        return max(state.stop_price, candidate)
    return candidate


def daily_pct_trailing_stop(state: StopState, *, pct: float) -> float | None:
    if state.highest_since_buy <= 0:
        return state.stop_price
    return state.highest_since_buy * (1.0 - pct)


def daily_breakeven_stop(state: StopState, *, entry_price: float) -> float | None:
    """保本止损的 T 日触发价（T-1 最高价判定激活）。"""
    if state.module_state.get("activated"):
        return entry_price
    trigger = float(state.module_state.get("trigger_atr", 1.0))
    if state.atr_at_entry > 0 and state.highest_since_buy - entry_price > trigger * state.atr_at_entry:
        state.module_state["activated"] = True
        return entry_price
    return state.stop_price


# ----------------------------------------------------------------------
# 当日结算后的状态并入（供次日判定）
# ----------------------------------------------------------------------


def post_day_update(state: StopState, *, day_high: float, day_low: float | None = None) -> None:
    if np.isfinite(day_high):
        state.highest_since_buy = max(state.highest_since_buy, float(day_high))
    if "held_days" in state.module_state:
        state.module_state["held_days"] = int(state.module_state["held_days"]) + 1


def ma_value(bars_df: pd.DataFrame, period: int) -> float | None:
    """截至序列末端的 SMA（收盘确认型止损的参考线）。"""
    if bars_df is None or bars_df.empty or len(bars_df) < period:
        return None
    series = _sma(pd.to_numeric(bars_df["close"], errors="coerce"), period)
    if series.empty:
        return None
    last = float(series.iloc[-1])
    return last if np.isfinite(last) else None


def donchian_low(bars_df: pd.DataFrame, period: int) -> float | None:
    """最近 period 根 bar 的最低价（唐奇安/前低止损通道）。"""
    if bars_df is None or bars_df.empty:
        return None
    lows = pd.to_numeric(bars_df["low"], errors="coerce").tail(period)
    if lows.empty:
        return None
    value = float(lows.min())
    return value if np.isfinite(value) else None
