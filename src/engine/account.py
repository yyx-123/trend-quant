"""账户记账：逐日结算、T+1 可卖滚动、组合热（详设 §4.4）。

- 收盘估值：cash / positions_value / equity / exposure 逐日结算；
- **组合热** = Σ max(0, (现价 − stop_price) × quantity)——存量度量；
  收盘确认型止损用其当日参考线作近似止损价计入 heat（报告标注
  approximate）；真正无止损的持仓（position_risk=none）记 None 并告警；
- **空仓资金按固定年化 1% 计息**（国债逆回购近似利率；写死常量，
  不做动态货基利率、不调参）；无融资；
- T+1：当日买入计入 quantity、不计入 sellable_quantity；日结后滚动
  sellable ← quantity。
"""

from __future__ import annotations

from engine.models import Account, Fill, Position

TRADING_DAYS_PER_YEAR = 252


def apply_buy_fill(account: Account, fill: Fill, *, entry_state, position_risk_ref: str) -> Position:
    """买入成交入账：现金扣减、持仓建立（sellable=0，T+1）。"""
    account.cash = fill.cash_after
    position = Position(
        symbol=fill.symbol,
        quantity=fill.quantity,
        sellable_quantity=0,
        avg_cost=(fill.fill_price * fill.quantity + fill.fee_total) / fill.quantity,
        entry_date=fill.fill_date,
        entry_price=fill.fill_price,  # 实际买入价 = 止损基准（决策 6）
        stop=entry_state,
        position_risk_ref=position_risk_ref,
    )
    account.positions[fill.symbol] = position
    return position


def apply_sell_fill(account: Account, fill: Fill) -> None:
    """卖出成交入账：现金入账、持仓清零（MVP 整仓全清，无部分卖出）。"""
    account.cash = fill.cash_after
    pos = account.positions.get(fill.symbol)
    if pos is None:
        return
    pos.quantity -= fill.quantity
    pos.sellable_quantity = max(0, pos.sellable_quantity - fill.quantity)
    if pos.quantity <= 0:
        del account.positions[fill.symbol]


def rollover_t1(account: Account) -> None:
    """T+1 可卖滚动：在**新交易日的开始**调用，昨日持仓全部转为可卖。

    不能在当日 settle 时滚动——否则当日买入的持仓在日结快照里就显示为
    可卖，T+1 名存实亡（engine_positions 快照须如实记录"当日买入
    sellable=0"）。
    """
    for pos in account.positions.values():
        pos.sellable_quantity = pos.quantity


def settle_day(
    account: Account,
    *,
    day,
    close_prices: dict[str, float],
    cash_interest_rate: float,
) -> dict:
    """日结：计息 → NAV/heat/exposure 行（T+1 滚动见 rollover_t1）。"""
    # 空仓资金计息（按日计提年化 1%，写入现金）
    interest = account.cash * (cash_interest_rate / TRADING_DAYS_PER_YEAR)
    if interest > 0:
        account.cash += interest

    positions_value = account.positions_value(close_prices)
    equity = account.cash + positions_value
    heat = account.heat(close_prices)
    return {
        "date": day.isoformat() if hasattr(day, "isoformat") else str(day),
        "cash": float(account.cash),
        "positions_value": float(positions_value),
        "equity": float(equity),
        "heat": None if heat is None else float(heat),
        "exposure": float(positions_value / equity) if equity > 0 else 0.0,
        "interest": float(interest),
    }
