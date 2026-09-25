"""市场规则集（market profile，详设 §4.2.1）。

费用与交易规则不写死在引擎里，打包为市场规则集；引擎（fees/matcher/
account）只从 profile 读参数。首版只实现 `cn_stock`（沪深股票/ETF 现行
规则）；未来加可转债/港股/期货时新增 profile，不改引擎。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketProfile:
    name: str
    commission_rate: float          # 佣金费率（双边）
    commission_min: float           # 最低佣金（元）
    stamp_tax_sell_stock: float     # 印花税（卖出·股票）
    stamp_tax_sell_etf: float       # 印花税（卖出·ETF）
    lot_size: int                   # 整手单位（买入对齐、卖出全清）
    t_plus: int                     # 结算约定（1 = T+1 可卖）
    cash_interest_rate: float       # 空仓资金年化利率（写死常量，不做动态货基利率）

    def stamp_tax_rate(self, asset_type: str) -> float:
        return (
            self.stamp_tax_sell_etf
            if str(asset_type or "").lower() == "etf"
            else self.stamp_tax_sell_stock
        )


# cn_stock：沪深股票/ETF 现行规则（详设 §2.4 费用规则沿用现有模型）。
CN_STOCK = MarketProfile(
    name="cn_stock",
    commission_rate=0.0000854,   # 万 0.854 双边
    commission_min=5.0,          # 最低 5 元
    stamp_tax_sell_stock=0.0005, # 卖出 0.05%，仅股票
    stamp_tax_sell_etf=0.0,      # ETF 免
    lot_size=100,
    t_plus=1,
    cash_interest_rate=0.01,     # 空仓资金按固定年化 1% 计息（国债逆回购近似；2026-09-23 用户定：写死）
)

# 分品种最小申报数量（R16-D-1）：科创板（688xxx）限价/市价申报单笔**不小于 200 股**
# （超 200 股部分可按 1 股递增）；其余品种 100 股/份起。旧实现一律按 lot_size 对齐，
# 在科创板会产出 100 股委托——券商必然拒单，却被记账成交（实测 match_buy 对
# 688498.SS 返回 filled qty=100）→ 持仓/现金/NAV 与台账全部偏离。
STAR_MIN_ORDER_QTY = 200


def min_buy_qty(symbol: str, *, asset_type: str | None = None, lot_size: int = 100) -> int:
    """买入的最小申报数量（分品种）。科创板股票 = 200，其余 = lot_size（默认 100）。"""
    from core.symbols import symbol_suffix, symbol_to_code

    if str(asset_type or "").lower() == "stock" and symbol_suffix(symbol) == "SS"             and symbol_to_code(symbol).startswith("68"):
        return STAR_MIN_ORDER_QTY
    return max(int(lot_size), 1)


_PROFILES = {CN_STOCK.name: CN_STOCK}


def get_profile(name: str = "cn_stock") -> MarketProfile:
    profile = _PROFILES.get(str(name or "").strip())
    if profile is None:
        raise KeyError(f"unknown market profile: {name}")
    return profile
