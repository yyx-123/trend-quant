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
    # 止损跳空成交修正（方案 2026-08-30 §7.1）：止损触发日若开盘价已穿透
    # 止损价，按开盘价成交（更差价格）而非假设在止损价成交。默认开 ——
    # 紧止损对该假设最敏感，关掉等于系统性美化紧档。
    stop_gap_fill: bool = True

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
    # —— round-trip 采集（方案 2026-08-30 §2）：持仓期逐日维护，卖出时合成记录 ——
    # 入场 bar 下标（all_bars 坐标系），holding_days/post-exit 扫描的定位锚。
    entry_bar_idx: int | None = None
    # 持有期最低价（含入场日），MAE 的直接来源；0 表示未初始化。
    mae_price: float = 0.0
    # 本笔实际使用的硬止损 ATR 倍数（initialize_stop_state 时从 exit spec 捕获），
    # R 倍数 = pnl / (qty × hard_stop_atr_mul × atr_at_entry) 的分母依据。
    hard_stop_atr_mul: float = 0.0
    # 本笔实际使用的吊灯止损 ATR 倍数（同为 initialize 时捕获，紧/松档溯源用）。
    chandelier_atr_mul: float = 0.0

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
        self.entry_bar_idx = None
        self.mae_price = 0.0
        self.hard_stop_atr_mul = 0.0
        self.chandelier_atr_mul = 0.0
        # last_exit_bar_idx 不在此清除 —— 见字段注释。


@dataclass(slots=True)
class RoundTrip:
    """一次往返交易（BUY→SELL 配对）的诊断记录（方案 2026-08-30 §2.2）。

    引擎在卖出成交时合成；批量服务层随后补记 category_l1 与实际止损倍数。
    post_exit_* 字段在回测区间末尾出场时为 None（未来 K 线不足），聚合跳过。
    """

    symbol: str
    entry_date: str
    entry_price: float          # 含滑点成交价
    exit_date: str
    exit_price: float
    exit_reason: str            # hard_stop / chandelier_stop / chandelier_stop_ratchet / exit_conditions_passed
    qty: int
    pnl: float                  # 净盈亏（扣费用）
    r_multiple: float | None    # pnl / (qty × hard_stop_atr_mul × atr_at_entry)；分母≤0 时 None
    mae_pct: float              # 持有期最低价相对入场价的最大不利偏移 %
    mfe_pct: float              # 持有期最高价相对入场价的最大有利偏移 %
    mae_atr: float | None       # 同上，单位 = 入场 ATR（atr_at_entry≤0 时 None）
    mfe_atr: float | None
    holding_days: int           # 入场到出场相隔交易日数
    entry_atr: float            # 入场时 ATR，T-1 收盘口径（atr_basis=prev_close）
    entry_atr_pct: float        # entry_atr / entry_price，波动率分桶的直接依据
    asset_type: str             # stock / etf
    days_to_trigger: int        # 入场到出场相隔交易日数（止损触发时间分布用，= holding_days）
    post_exit_ret_5d: float | None = None   # 出场后第 5/10/20 个交易日收盘相对出场价
    post_exit_ret_10d: float | None = None
    post_exit_ret_20d: float | None = None
    reentry_above_entry_5d: bool | None = None   # 5/10/20 日内收盘是否重新站回入场价（假止损判据）
    reentry_above_entry_10d: bool | None = None
    reentry_above_entry_20d: bool | None = None
    # —— 以下由批量服务层补记（引擎不关心）——
    category_l1: str = ""
    hard_stop_atr_mul: float = 0.0     # 本笔实际使用的硬止损倍数（紧/松档溯源）
    chandelier_atr_mul: float = 0.0
