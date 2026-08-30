from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from rule_backtest.indicators import atr, latest_field
from rule_backtest.models import PositionState

# Optional memoized ATR lookup: (day_idx, period) -> value | None.
# When provided, stop-state ATR is an indexed lookup instead of a
# per-day full rolling recompute (P1.3).
AtrLookup = Callable[[int, int], float | None]


def _atr_value(
    bars: pd.DataFrame,
    period: int,
    atr_at: AtrLookup | None,
    lookback: int = 0,
) -> tuple[float | None, dict]:
    """ATR 取值；lookback>0 时向前回看 N 根（入场 ATR 用 T-1 收盘口径，见
    initialize_stop_state）。memoized 与 legacy 两条路径必须给出同一值
    （P1.3 golden 保证），故 lookback 同时作用于两条路径。"""
    if atr_at is not None:
        idx = len(bars) - 1 - int(lookback)
        value = atr_at(idx, period) if idx >= 0 else None
        if value is not None:
            return value, {}
        # Lookup unavailable (no memoization context) or value missing:
        # fall back to the legacy per-day computation for a correct answer.
    base = bars.iloc[: len(bars) - int(lookback)] if lookback else bars
    return atr(base, period=period)


def update_position_state_for_day(
    position: PositionState,
    bars: pd.DataFrame,
    strategy: dict,
    atr_at: AtrLookup | None = None,
) -> dict:
    if not position.is_open:
        return {}

    high = latest_field(bars, "high")
    if high is not None:
        if position.highest_high_since_entry <= 0:
            position.highest_high_since_entry = float(high)
        else:
            position.highest_high_since_entry = max(position.highest_high_since_entry, float(high))

    # MAE 跟踪（round-trip 采集）：持有期逐日最低价，MFE 复用 highest_high_since_entry。
    low = latest_field(bars, "low")
    if low is not None:
        if position.mae_price <= 0:
            position.mae_price = float(low)
        else:
            position.mae_price = min(position.mae_price, float(low))

    trace: dict = {"highest_high_since_entry": position.highest_high_since_entry}
    exit_group = strategy.get("exit", {}) if isinstance(strategy.get("exit", {}), dict) else {}
    for condition in exit_group.get("children", []) or []:
        for side in ("left", "right"):
            spec = condition.get(side, {}) if isinstance(condition, dict) else {}
            if not isinstance(spec, dict) or spec.get("type") != "state_value":
                continue
            name = spec.get("name")
            if name not in ("chandelier_stop", "chandelier_stop_ratchet"):
                continue
            params = spec.get("params", {}) if isinstance(spec.get("params", {}), dict) else {}
            atr_period = int(params.get("atr_period", 20))
            atr_mul = float(params.get("atr_mul", 2.5))
            atr_value, atr_trace = _atr_value(bars, atr_period, atr_at)
            if atr_value is not None and position.highest_high_since_entry > 0:
                candidate = position.highest_high_since_entry - atr_mul * atr_value
                if name == "chandelier_stop_ratchet":
                    # 棘轮版：只上移不下移，与前一日止损价取 max。
                    position.chandelier_stop_ratchet = max(position.chandelier_stop_ratchet, candidate)
                else:
                    position.chandelier_stop = candidate
            trace[name] = getattr(position, name)
            trace["chandelier_atr"] = atr_trace
    return trace


def initialize_stop_state(
    position: PositionState,
    bars: pd.DataFrame,
    strategy: dict,
    entry_price: float,
    entry_date: str,
    atr_at: AtrLookup | None = None,
    atr_lookback: int = 1,
) -> dict:
    """入场时初始化止损状态。

    atr_lookback=1（默认，方案 2026-08-30 §7.3）：入场 ATR 取 T-1 收盘已完成
    的值 —— 入场决策在盘中做出时当根 K 线尚未收完，含当根是轻微未来函数，
    且波动大的日子 ATR 被当日拉大、止损被放得更松。实盘 stop_loss.py 是盘中
    实时守护，含当根有其合理性，口径不动；两套口径差异写入导出 manifest。
    入场日无前一根 K 线时 ATR 缺失 → hard_stop=0（本笔无硬止损），与旧版
    「ATR 周期不足 → 无止损」行为一致。
    """
    position.entry_price = float(entry_price)
    position.entry_date = entry_date
    high = latest_field(bars, "high")
    position.highest_high_since_entry = float(high if high is not None else entry_price)
    low = latest_field(bars, "low")
    position.mae_price = float(min(low, entry_price) if low is not None else entry_price)

    trace: dict = {"entry_price": entry_price, "entry_date": entry_date, "atr_basis": "prev_close" if atr_lookback else "entry_bar"}
    exit_group = strategy.get("exit", {}) if isinstance(strategy.get("exit", {}), dict) else {}
    for condition in exit_group.get("children", []) or []:
        for side in ("left", "right"):
            spec = condition.get(side, {}) if isinstance(condition, dict) else {}
            if not isinstance(spec, dict) or spec.get("type") != "state_value":
                continue
            params = spec.get("params", {}) if isinstance(spec.get("params", {}), dict) else {}
            name = str(spec.get("name", "")).strip()
            if name == "hard_stop":
                atr_period = int(params.get("atr_period", 20))
                atr_mul = float(params.get("atr_mul", 1.5))
                atr_value, atr_trace = _atr_value(bars, atr_period, atr_at, lookback=atr_lookback)
                position.atr_at_entry = float(atr_value or 0.0)
                position.hard_stop_atr_mul = atr_mul
                position.hard_stop = entry_price - atr_mul * position.atr_at_entry if atr_value is not None else 0.0
                trace["hard_stop"] = position.hard_stop
                trace["hard_stop_atr"] = atr_trace
            elif name in ("chandelier_stop", "chandelier_stop_ratchet"):
                atr_period = int(params.get("atr_period", 20))
                atr_mul = float(params.get("atr_mul", 2.5))
                atr_value, atr_trace = _atr_value(bars, atr_period, atr_at, lookback=atr_lookback)
                position.chandelier_atr_mul = atr_mul
                if atr_value is not None:
                    # 买入当日无前值可比，棘轮版与原版同为直接赋值。
                    setattr(position, name, position.highest_high_since_entry - atr_mul * atr_value)
                trace[name] = getattr(position, name)
                trace["chandelier_atr"] = atr_trace
    return trace
