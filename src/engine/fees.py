"""费用计算（详设 §2.4/§4.2）：佣金双边万 0.854 最低 5 元、印花税卖出收
（股票 0.05%、ETF 免）、基础滑点 + 尾盘执行滑点（买卖不利方向）。

所有规则参数来自 MarketProfile——本模块没有任何写死的数字。
"""

from __future__ import annotations

from engine.profiles import MarketProfile


def commission(gross: float, profile: MarketProfile) -> float:
    return max(gross * profile.commission_rate, profile.commission_min)


def buy_cost(
    *,
    base_price: float,
    slippage_base: float,
    slippage_tail: float,
    quantity: int,
    profile: MarketProfile,
    asset_type: str,
) -> dict:
    """买入成交明细。fill_price = 基准价 × (1 + 基础滑点 + 尾盘滑点)。"""
    fill_price = base_price * (1.0 + slippage_base + slippage_tail)
    gross = quantity * fill_price
    comm = commission(gross, profile)
    stamp = 0.0  # 买入不收印花税
    return {
        "fill_price": fill_price,
        "gross": gross,
        "commission": comm,
        "stamp_tax": stamp,
        "fee_total": comm + stamp,
        "total_cost": gross + comm + stamp,
    }


def sell_proceeds(
    *,
    reference_price: float,
    slippage_base: float,
    slippage_tail: float,
    quantity: int,
    profile: MarketProfile,
    asset_type: str,
    apply_slippage: bool = True,
) -> dict:
    """卖出成交明细。fill_price = 基准价 × (1 − 滑点)；印花税卖出收。

    apply_slippage=False 用于止损盘中触及成交（详设 §4.2：按 stop_price
    成交/跳空按开盘价成交——条件单触发价即成交假设，不另叠滑点）。
    """
    slip = (slippage_base + slippage_tail) if apply_slippage else 0.0
    fill_price = reference_price * (1.0 - slip)
    gross = quantity * fill_price
    comm = commission(gross, profile)
    stamp = gross * profile.stamp_tax_rate(asset_type)
    return {
        "fill_price": fill_price,
        "gross": gross,
        "commission": comm,
        "stamp_tax": stamp,
        "fee_total": comm + stamp,
        "net_proceeds": gross - comm - stamp,
    }


def max_affordable_quantity(
    *,
    cash: float,
    exec_price_est: float,
    profile: MarketProfile,
) -> int:
    """现金约束下整手对齐的最大可买数量（逐手递减校验含费）。

    与旧引擎 ``_max_buy_qty`` 同数学（口径锚定，parity 用）。
    """
    if cash <= 0 or exec_price_est <= 0:
        return 0
    lot = max(int(profile.lot_size), 1)
    estimated_per_share = exec_price_est * (1.0 + profile.commission_rate)
    raw_qty = int(cash // estimated_per_share)
    qty = (raw_qty // lot) * lot
    while qty > 0:
        gross = qty * exec_price_est
        comm = commission(gross, profile)
        if gross + comm <= cash:
            return qty
        qty -= lot
    return 0
