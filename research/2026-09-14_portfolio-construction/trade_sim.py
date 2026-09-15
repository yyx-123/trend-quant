"""交易模拟器：与 rule_backtest 引擎逐条对齐的执行语义。

存在的理由：研究需要评估「所有候选入场」的前瞻结果，而引擎（按设计）只会
记录它实际采取的那一笔 —— 无法用来比较「选 A 还是选 B」。所以这里复制一份
执行语义，并用 verify_simulator.py 对真引擎逐笔对账（exit 日期与盈亏必须完全一致）。

对齐要点（源码位置 src/rule_backtest/engine.py）：
  - 买入参考价 = 当日收盘；成交价 = 收盘 × (1 + slippage)          (_execute_buy)
  - 入场 ATR = T-1 收盘的 ATR20（规避未来函数，方案 2026-08-30 §7.3）
  - hard_stop = 成交价 − 1.5 × ATR(T-1)，全程固定不动             (initialize_stop_state)
  - chandelier = max(high[entry..d]) − 2.5 × ATR20(d)，每日重算，可下移
  - 出场判定顺序：先 hard_stop 后 chandelier；触发价即成交参考价
    （不是收盘价），若当日开盘已穿价则按开盘价成交（stop_gap_fill）  
  - 卖出成交价 = 参考价 × (1 − slippage)；股票另收 0.1% 印花税       (_execute_sell)
  - 手续费 = max(成交额 × fee_rate, fee_min)
  - 入场当日不判出场（引擎中「平仓判定」在「买入」之前）
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SLIPPAGE = 0.002
FEE_RATE = 0.0000854
FEE_MIN = 5.0
LOT_SIZE = 100
STOCK_STAMP_TAX_RATE = 0.001
ATR_PERIOD = 20
HARD_STOP_ATR_MUL = 1.5
CHANDELIER_ATR_MUL = 2.5


@dataclass(slots=True)
class SymbolArrays:
    """单标的的 numpy 视图 —— 逐笔循环里避免 pandas 取值开销。"""

    symbol: str
    date: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    asset_type: str
    #: 特征列，键为列名 —— 入场日的取值直接随交易记录一起返回
    features: dict[str, np.ndarray]

    def __len__(self) -> int:
        return len(self.close)


@dataclass(slots=True)
class TradeResult:
    symbol: str
    entry_idx: int
    entry_date: str
    exit_idx: int
    exit_date: str
    exit_reason: str
    holding_days: int
    ret_net_pct: float      # 净收益 / 投入成本
    pnl: float
    r_multiple: float | None  # pnl / (qty × 1.5 × ATR_entry)
    mae_pct: float          # 持有期最低价相对成交价的偏移
    mfe_pct: float
    mfe_r: float | None     # 最大有利偏移 / 1R —— 「先赚过多少」的度量
    features: dict[str, float]
    force_closed: bool      # 触达 max_horizon 强平（非策略真实出场）


def buy_cost(close: float, qty: int, slippage: float = SLIPPAGE) -> tuple[float, float]:
    """返回 (成交价, 总成本含费)。"""
    exec_price = close * (1.0 + slippage)
    gross = qty * exec_price
    commission = max(gross * FEE_RATE, FEE_MIN)
    return exec_price, gross + commission


def _qty_for_notional(close: float, notional: float, slippage: float = SLIPPAGE) -> int:
    per_share = close * (1.0 + slippage) * (1.0 + FEE_RATE)
    qty = int(notional // per_share) if per_share > 0 else 0
    return (qty // LOT_SIZE) * LOT_SIZE


def simulate_entry(
    arr: SymbolArrays,
    entry_idx: int,
    *,
    notional: float = 10_000.0,
    hard_mul: float = HARD_STOP_ATR_MUL,
    chand_mul: float = CHANDELIER_ATR_MUL,
    max_horizon: int = 250,
    slippage: float = SLIPPAGE,
) -> TradeResult | None:
    """模拟「第 entry_idx 日收盘入场」的完整一笔。

    返回 None 表示该入场点不可用（ATR 预热不足 / 资金不足一手 / 区间末尾无数据）。

    ``slippage`` 可覆盖：0.002 是引擎默认（对小额/低流动性标的偏保守），
    流动性好的宽基 ETF 真实一跳约 0.03%，故敏感度必须检验。
    """
    n = len(arr)
    if entry_idx < 1 or entry_idx >= n:
        return None
    atr_entry = arr.atr[entry_idx - 1]  # T-1 口径
    if not np.isfinite(atr_entry) or atr_entry <= 0:
        return None

    entry_close = float(arr.close[entry_idx])
    qty = _qty_for_notional(entry_close, notional, slippage)
    if qty <= 0:
        return None
    entry_exec, entry_cost = buy_cost(entry_close, qty, slippage)
    hard_stop = entry_exec - hard_mul * float(atr_entry)

    highest = float(arr.high[entry_idx])
    lowest = min(float(arr.low[entry_idx]), entry_exec)
    mfe_price = max(float(arr.high[entry_idx]), entry_exec)

    exit_idx = -1
    exit_reason = ""
    ref_price = 0.0
    for d in range(entry_idx + 1, min(n, entry_idx + 1 + max_horizon)):
        highest = max(highest, float(arr.high[d]))
        lowest = min(lowest, float(arr.low[d]))
        mfe_price = max(mfe_price, float(arr.high[d]))
        atr_d = arr.atr[d]
        chand = highest - chand_mul * float(atr_d) if np.isfinite(atr_d) else np.nan

        close_d = float(arr.close[d])
        if close_d <= hard_stop:
            exit_reason, ref_price = "hard_stop", hard_stop
        elif np.isfinite(chand) and close_d <= chand:
            exit_reason, ref_price = "chandelier_stop", float(chand)
        if not exit_reason:
            continue
        # 跳空修正：止损触发日开盘已穿价 → 按开盘价成交
        open_d = float(arr.open[d])
        if 0 < open_d < ref_price:
            ref_price = open_d
        exit_idx = d
        break

    force_closed = False
    if exit_idx < 0:
        # 区间末尾仍未触发止损 —— 按最后一根收盘平仓，用于统计可比（标记为强平）
        exit_idx = min(n - 1, entry_idx + max_horizon)
        exit_reason = "force_close"
        ref_price = float(arr.close[exit_idx])
        force_closed = True

    exit_exec = ref_price * (1.0 - slippage)
    gross = qty * exit_exec
    commission = max(gross * FEE_RATE, FEE_MIN)
    stamp = gross * STOCK_STAMP_TAX_RATE if arr.asset_type == "stock" else 0.0
    net = gross - commission - stamp
    pnl = net - entry_cost

    risk_unit = qty * hard_mul * float(atr_entry)
    return TradeResult(
        symbol=arr.symbol,
        entry_idx=entry_idx,
        entry_date=str(arr.date[entry_idx])[:10],
        exit_idx=exit_idx,
        exit_date=str(arr.date[exit_idx])[:10],
        exit_reason=exit_reason,
        holding_days=exit_idx - entry_idx,
        ret_net_pct=pnl / entry_cost if entry_cost > 0 else 0.0,
        pnl=pnl,
        r_multiple=pnl / risk_unit if risk_unit > 0 else None,
        mae_pct=lowest / entry_exec - 1.0,
        mfe_pct=mfe_price / entry_exec - 1.0,
        mfe_r=(mfe_price - entry_exec) / (hard_mul * float(atr_entry)),
        features={k: float(v[entry_idx]) for k, v in arr.features.items()},
        force_closed=force_closed,
    )


def to_arrays(panel: pd.DataFrame, feature_cols: list[str]) -> dict[str, SymbolArrays]:  # noqa: F821
    """把长面板切成逐标的 numpy 视图。"""
    import pandas as pd  # noqa: F401

    out: dict[str, SymbolArrays] = {}
    for symbol, g in panel.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        out[symbol] = SymbolArrays(
            symbol=symbol,
            date=g["date"].astype("datetime64[ns]").to_numpy(),
            open=g["open"].to_numpy(dtype=float),
            high=g["high"].to_numpy(dtype=float),
            low=g["low"].to_numpy(dtype=float),
            close=g["close"].to_numpy(dtype=float),
            atr=g["atr"].to_numpy(dtype=float),
            asset_type=str(g["asset_type"].iloc[0] or "etf"),
            features={c: g[c].to_numpy(dtype=float) for c in feature_cols if c in g.columns},
        )
    return out
