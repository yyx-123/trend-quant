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

_PROFILES = {CN_STOCK.name: CN_STOCK}


def get_profile(name: str = "cn_stock") -> MarketProfile:
    profile = _PROFILES.get(str(name or "").strip())
    if profile is None:
        raise KeyError(f"unknown market profile: {name}")
    return profile
