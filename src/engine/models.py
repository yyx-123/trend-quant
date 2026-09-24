"""核心模型（详设 §2.4/§4.1，对照 vnpy / LEAN / backtrader）。

Order（订单=意图；决策日与成交日分列，尾盘口径的审计需要）；
Fill（成交=事实，费用分列）；Position（T+1 一等表达：可卖数量是显式
字段，参照 vnpy yd_volume）；Account（现金/持仓/净值/组合热派生）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 未成交原因（"错过的交易"本身是研究素材）
UNFILLED_REASONS: tuple[str, ...] = (
    "limit_up",
    "limit_down",
    "suspended",
    "t1_block",
    "insufficient_cash",
    "lot_rounding",
)

ORDER_SOURCES: tuple[str, ...] = ("signal", "stop", "rotation", "rebalance")


@dataclass(slots=True)
class OrderIntent:
    """sizing 槽产出的买入意图（L3 → L2）。"""

    symbol: str
    decision_date: date
    intent_type: str = "quantity"  # quantity | target_value（target_weight 预留经 target_value 表达）
    value: float = 0.0
    source: str = "signal"


@dataclass(slots=True)
class ExitOrderIntent:
    """持仓风控/信号/轮换产出的卖出意图（L3 → L2）。

    fill_mode（协议补强②，详设 §4.3.1）：
    - intraday_stop：盘中触及型——引擎盘中盯守 stop_price，触及即成交
      （跳空按开盘价；跌停/停牌/T+1 阻塞记 unfilled 并次日重试）；
    - tail：收盘确认型——条件成立按尾盘口径成交（如"收盘跌破均线才算"）。
    """

    symbol: str
    decision_date: date
    fill_mode: str = "intraday_stop"  # intraday_stop | tail
    stop_price: float | None = None
    source: str = "stop"  # stop | signal | rotation
    reason: str = "stop"


@dataclass(slots=True)
class StopState:
    """持仓风控状态（随 engine_positions 逐日快照落库）。

    固定字段（stop_price/highest_since_buy/atr_at_entry）为公共部分；
    ``module_state`` 是不透明 dict——PSAR 加速因子、Supertrend 方向位、
    保本止损激活位等模块自定义状态（协议补强①，详设 §4.3.1）。
    """

    stop_price: float | None
    highest_since_buy: float
    atr_at_entry: float
    fill_mode: str = "intraday_stop"  # heat 口径需要：收盘确认型 heat 记近似
    heat_approximate: bool = False
    module_state: dict = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int
    sellable_quantity: int  # T+1：当日买入计入 quantity 但不计入 sellable
    avg_cost: float         # 含买入费用（total_cost / qty）
    entry_date: date
    entry_price: float      # 实际买入价 = 止损基准（决策 6）
    stop: StopState | None = None
    position_risk_ref: str = ""  # 产生该持仓风控状态的模块（none@1 时也记录）

    @property
    def is_open(self) -> bool:
        return self.quantity > 0


@dataclass(slots=True)
class Fill:
    order_id: str
    symbol: str
    fill_date: date
    base_price: float
    slippage_base: float
    slippage_tail: float
    fill_price: float
    quantity: int
    commission: float
    stamp_tax: float
    fee_total: float
    cash_after: float


@dataclass(slots=True)
class Unfilled:
    order_id: str
    symbol: str
    decision_date: date
    reason: str
    intent_snapshot: dict


@dataclass(slots=True)
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def positions_value(self, close_prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            price = close_prices.get(symbol)
            if price is None or price <= 0:
                continue
            total += pos.quantity * price
        return total

    def equity(self, close_prices: dict[str, float]) -> float:
        return self.cash + self.positions_value(close_prices)

    def exposure(self, close_prices: dict[str, float]) -> float:
        eq = self.equity(close_prices)
        if eq <= 0:
            return 0.0
        return self.positions_value(close_prices) / eq

    def unstopped_symbols(self) -> list[str]:
        """无止损价的持仓清单（heat 的"未知风险"组成部分；报告/闸门据此告警）。"""
        return [
            symbol for symbol, pos in self.positions.items()
            if pos.stop is None or pos.stop.stop_price is None
        ]

    def heat(self, close_prices: dict[str, float]) -> float | None:
        """组合热 = Σ max(0, (现价 − stop_price) × quantity)。

        口径（详设 §4.4 + 评审 DS-P2-8）：任一持仓缺止损价 → 组合热不可知，
        记 None（不是"少计一点"——缺失部分必须显式可见，由 unstopped_symbols
        与报告/告警承担）；全部持仓有止损时返回精确合计；空仓返回 0。
        """
        if self.unstopped_symbols():
            return None
        total = 0.0
        for symbol, pos in self.positions.items():
            price = close_prices.get(symbol)
            if price is None or pos.stop is None or pos.stop.stop_price is None:
                continue
            total += max(0.0, (price - pos.stop.stop_price) * pos.quantity)
        return total
