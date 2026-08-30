"""旧批次 round-trip 回填的重放推导（方案 2026-08-30 §2.3 任务 8）。

旧批次没有 round_trips_json：用格子 trades_json + qfq 行情重放 BUY→SELL 配对，
补齐 MAE/MFE/R/post-exit 字段。与引擎采集的差异（导出 manifest 需注明）：
- 入场 ATR 重放为**含入场日当根**的旧口径（旧批次引擎本来就是这个口径），
  与新批次 prev_close 口径不同；
- MAE/MFE 用日内 high/low 重放，与引擎逐日跟踪等价；
- 日度 NAV 维度指标（cvar_5/ulcer_index/max_dd_duration_days）无法回填
  （daily_nav 未落库），保持 NULL。
"""

from __future__ import annotations

import pandas as pd

from core import indicators as core_ind

_STOP_SPEC_NAMES = {"hard_stop", "chandelier_stop", "chandelier_stop_ratchet"}
_POST_EXIT_WINDOWS = (5, 10, 20)


def _stop_spec_params(strategy_config: dict, name: str) -> tuple[int, float] | None:
    """从策略快照 exit spec 取止损状态值的 (atr_period, atr_mul)。"""
    exit_group = strategy_config.get("exit", {}) if isinstance(strategy_config.get("exit", {}), dict) else {}
    for condition in exit_group.get("children", []) or []:
        if not isinstance(condition, dict):
            continue
        for side in ("left", "right"):
            spec = condition.get(side, {})
            if isinstance(spec, dict) and spec.get("type") == "state_value" and spec.get("name") == name:
                params = spec.get("params", {}) if isinstance(spec.get("params", {}), dict) else {}
                return int(params.get("atr_period", 20)), float(
                    params.get("atr_mul", 1.5 if name == "hard_stop" else 2.5)
                )
    return None


def replay_round_trips(
    trades: list[dict],
    bars: pd.DataFrame,
    strategy_config: dict,
    *,
    asset_type: str = "etf",
    category_l1: str = "",
) -> list[dict]:
    """从 BUY/SELL 交易序列 + 行情重放 round-trip 记录（幂等纯函数）。

    trades 为格子 trades_json 解析结果（含 side/date/exec_price/pnl/reason）；
    bars 需含 time/date、high、low、close 列。
    """
    if bars.empty or not trades:
        return []
    df = bars.copy()
    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    df = df.sort_values("date").reset_index(drop=True)
    day_index = {str(d)[:10]: i for i, d in enumerate(df["date"])}
    closes = pd.to_numeric(df["close"], errors="coerce")
    highs = pd.to_numeric(df["high"], errors="coerce")
    lows = pd.to_numeric(df["low"], errors="coerce")

    hard_spec = _stop_spec_params(strategy_config, "hard_stop")
    atr_period, hard_mul = hard_spec if hard_spec else (20, 0.0)
    chandelier_spec = _stop_spec_params(strategy_config, "chandelier_stop") or _stop_spec_params(
        strategy_config, "chandelier_stop_ratchet"
    )
    chandelier_mul = chandelier_spec[1] if chandelier_spec else 0.0
    atr_series = core_ind.atr(df, period=atr_period) if hard_spec else None

    out: list[dict] = []
    entry: dict | None = None
    for trade in trades:
        side = str(trade.get("side", "")).upper()
        day = str(trade.get("date", ""))[:10]
        if side == "BUY":
            entry = trade
            continue
        if side != "SELL" or entry is None:
            continue
        entry_day = str(entry.get("date", ""))[:10]
        e_idx = day_index.get(entry_day)
        x_idx = day_index.get(day)
        if e_idx is None or x_idx is None or x_idx < e_idx:
            entry = None
            continue

        entry_price = float(entry.get("exec_price") or entry.get("price") or 0.0)
        exit_price = float(trade.get("exec_price") or trade.get("price") or 0.0)
        qty = int(trade.get("qty") or entry.get("qty") or 0)
        pnl = float(trade.get("pnl") or 0.0)
        # 旧口径：入场 ATR 含入场日当根 K 线（2026-08-30 前引擎行为）
        entry_atr = 0.0
        if atr_series is not None:
            v = atr_series.iloc[e_idx]
            entry_atr = float(v) if pd.notna(v) else 0.0
        mae_price = float(lows.iloc[e_idx : x_idx + 1].min())
        mfe_price = float(highs.iloc[e_idx : x_idx + 1].max())
        risk = qty * hard_mul * entry_atr

        rt: dict = {
            "symbol": str(trade.get("symbol", "")),
            "entry_date": entry_day,
            "entry_price": entry_price,
            "exit_date": day,
            "exit_price": exit_price,
            "exit_reason": str(trade.get("reason", "")),
            "qty": qty,
            "pnl": pnl,
            "r_multiple": (pnl / risk) if risk > 0 else None,
            "mae_pct": (mae_price / entry_price - 1.0) * 100.0 if entry_price > 0 else 0.0,
            "mfe_pct": (mfe_price / entry_price - 1.0) * 100.0 if entry_price > 0 else 0.0,
            "mae_atr": ((mae_price - entry_price) / entry_atr) if entry_atr > 0 else None,
            "mfe_atr": ((mfe_price - entry_price) / entry_atr) if entry_atr > 0 else None,
            "holding_days": int(x_idx - e_idx),
            "entry_atr": entry_atr,
            "entry_atr_pct": (entry_atr / entry_price) if entry_price > 0 else 0.0,
            "asset_type": asset_type,
            "days_to_trigger": int(x_idx - e_idx),
            "category_l1": category_l1,
            "hard_stop_atr_mul": hard_mul,
            "chandelier_atr_mul": chandelier_mul,
        }
        future = closes.iloc[x_idx + 1 :].tolist()
        for n in _POST_EXIT_WINDOWS:
            if len(future) >= n and exit_price > 0:
                window = [float(c) for c in future[:n]]
                rt[f"post_exit_ret_{n}d"] = window[-1] / exit_price - 1.0
                rt[f"reentry_above_entry_{n}d"] = any(c > entry_price for c in window)
            else:
                rt[f"post_exit_ret_{n}d"] = None
                rt[f"reentry_above_entry_{n}d"] = None
        out.append(rt)
        entry = None
    return out
