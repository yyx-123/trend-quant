from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rule_backtest.sizing.base import PositionSizer


PriceField = Literal["open", "high", "low", "close", "volume", "amount"]
Operator = Literal[">=", "<=", "cross_above", "cross_below"]
InstrumentType = Literal["etf", "stock"]


DEFAULT_FEE_RATE = 0.0000854


@dataclass(frozen=True, slots=True)
class BacktestExecutionConfig:
    initial_capital: float = 100_000.0
    signal_timing: str = "close"
    fill_timing: str = "close"
    fee_rate: float = DEFAULT_FEE_RATE
    fee_min: float = 5.0
    slippage: float = 0.002
    lot_size: int = 100
    instrument_type: InstrumentType = "etf"
    stock_stamp_tax_rate: float = 0.001
    debug_log_enabled: bool | None = None
    debug_auto_enable_max_days: int = 31

    def normalized(self) -> BacktestExecutionConfig:
        """零值费用参数归一为默认值：系统不提供零成本回测场景
        （与 service 层 ``or DEFAULT`` 语义一致，消灭 service/engine 双口径）。
        """
        from dataclasses import replace

        return replace(
            self,
            fee_rate=self.fee_rate if self.fee_rate > 0 else DEFAULT_FEE_RATE,
            fee_min=self.fee_min if self.fee_min > 0 else 5.0,
            slippage=self.slippage if self.slippage > 0 else 0.002,
            stock_stamp_tax_rate=(
                self.stock_stamp_tax_rate if self.stock_stamp_tax_rate > 0 else 0.001
            ),
        )


@dataclass(frozen=True, slots=True)
class RuleBacktestRequest:
    strategy: dict
    symbol: str
    bars: object
    start_date: date | None = None
    end_date: date | None = None
    execution: BacktestExecutionConfig = field(default_factory=BacktestExecutionConfig)
    run_id: str | None = None
    # Position sizer (仓位策略); None keeps the legacy all-in buy behavior.
    sizer: PositionSizer | None = None
    # Called once per processed bar as (day_number, total_days); None disables reporting.
    progress_callback: Callable[[int, int], None] | None = None


@dataclass(slots=True)
class PositionState:
    qty: int = 0
    entry_price: float = 0.0
    avg_cost: float = 0.0
    entry_date: str | None = None
    atr_at_entry: float = 0.0
    hard_stop: float = 0.0
    highest_high_since_entry: float = 0.0
    chandelier_stop: float = 0.0
    # 棘轮版吊灯止损：与 chandelier_stop 同公式（买入以来最高价 − ATR×倍数），
    # 但只上移不下移 —— 每日取 max(前一日棘轮止损价, 当日候选值)。
    chandelier_stop_ratchet: float = 0.0
    # 上次卖出所在的 bar 下标（all_bars 坐标系），供 days_since_last_exit
    # 状态值做「离场冷却期」判断。与止损状态不同：它属于账户级历史，
    # 跨持仓周期存活，reset() 刻意不清除；None 表示本轮回测从未卖出。
    last_exit_bar_idx: int | None = None

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    def reset(self) -> None:
        self.qty = 0
        self.entry_price = 0.0
        self.avg_cost = 0.0
        self.entry_date = None
        self.atr_at_entry = 0.0
        self.hard_stop = 0.0
        self.highest_high_since_entry = 0.0
        self.chandelier_stop = 0.0
        self.chandelier_stop_ratchet = 0.0
        # last_exit_bar_idx 不在此清除 —— 见字段注释。
