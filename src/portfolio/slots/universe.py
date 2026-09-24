"""`universe` 标的池插槽（详设 §5.2.1）。

protocol UniverseProvider: members(ctx) -> list[UniverseMember]
返回成员需带类目（组合风控的集中度卡控要用）。

⚠️ 数据依赖：`exclude_st` 在阶段 7 前不生效（无 ST 状态历史）——历史回测
无法剔除"当年是 ST"的标的；该参数仅对未来（当日）过滤有效，研究报告/
verdict 必须带"未剔除历史 ST"警告（由 verdict 流水线的 warnings 承担）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from portfolio.registry import REGISTRY, ModuleSpec


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str = ""
    asset_type: str = ""


def _member_from_meta(symbol: str, meta: dict | None) -> UniverseMember:
    meta = meta or {}
    return UniverseMember(
        symbol=symbol,
        category_l1=str(meta.get("category_l1") or ""),
        category_l2=str(meta.get("category_l2") or ""),
        category_l3=str(meta.get("category_l3") or ""),
        asset_type=str(meta.get("asset_type") or ""),
    )


class StaticListUniverse:
    """显式清单。"""

    def __init__(self, params: dict) -> None:
        self.symbols = [str(s).strip().upper() for s in params.get("symbols", [])]

    def members(self, ctx) -> list[UniverseMember]:
        meta = ctx.gateway.metadata.instruments(self.symbols)
        return [_member_from_meta(s, meta.get(s)) for s in self.symbols]


class CategoryFilterUniverse:
    """三级类目 + enabled + asset_type 过滤（只做 ETF / 只做股票 = 一个参数）。"""

    def __init__(self, params: dict) -> None:
        self.levels = params.get("levels") or {}  # {"category_l1": [...], ...}
        self.asset_type = str(params.get("asset_type") or "all")
        self.exclude_st = bool(params.get("exclude_st", False))
        self.enabled_only = bool(params.get("enabled_only", True))
        self._meta_cache: dict | None = None

    def _meta(self, ctx) -> dict:
        # run 期数据冻结（决策 A3）：元数据在一次运行内不变，实例级缓存
        if self._meta_cache is None:
            self._meta_cache = ctx.gateway.metadata.instruments()
        return self._meta_cache

    def members(self, ctx) -> list[UniverseMember]:
        meta = self._meta(ctx)
        out = []
        for symbol, row in meta.items():
            if self.enabled_only and not row.get("enabled", 1):
                continue
            if self.asset_type != "all" and str(row.get("asset_type") or "").lower() != self.asset_type:
                continue
            ok = True
            for level, wanted in self.levels.items():
                if str(row.get(level) or "") not in set(wanted):
                    ok = False
                    break
            if not ok:
                continue
            # exclude_st：阶段 7 前无 ST 状态历史——名字含 "ST" 的当日过滤
            # （仅当前状态有效，历史回测无法剔除"当年是 ST"）。
            if self.exclude_st and "ST" in str(row.get("name") or "").upper():
                continue
            out.append(_member_from_meta(symbol, row))
        return out


class LiquidityFilterUniverse:
    """近 N 日成交额 ≥ 阈值（等风险世界里的"可交易池"）。

    params: min_amount20（近 20 日成交额均值下限）、top_n（可选：按成交额
    均值取前 N）、levels/asset_type/enabled_only 同 category_filter。
    """

    def __init__(self, params: dict) -> None:
        self.min_amount20 = float(params.get("min_amount20", 1e8))
        self.top_n = params.get("top_n")
        self.asset_type = str(params.get("asset_type") or "all")
        self.enabled_only = bool(params.get("enabled_only", True))
        self._meta_cache: dict | None = None

    def members(self, ctx) -> list[UniverseMember]:
        if self._meta_cache is None:
            self._meta_cache = ctx.gateway.metadata.instruments()
        meta = self._meta_cache
        amount = ctx.panel.matrix("amount")  # (t+1, N)
        panel_symbols = ctx.panel.symbols
        scores: dict[str, float] = {}
        if amount.shape[0] > 0:
            tail = amount[-20:, :]
            with np.errstate(all="ignore"):
                mean20 = np.nanmean(tail, axis=0)
            for j, symbol in enumerate(panel_symbols):
                v = mean20[j]
                if np.isfinite(v):
                    scores[symbol] = float(v)
        out: list[tuple[str, float]] = []
        for symbol, score in scores.items():
            row = meta.get(symbol)
            if row is None:
                continue
            if self.enabled_only and not row.get("enabled", 1):
                continue
            if self.asset_type != "all" and str(row.get("asset_type") or "").lower() != self.asset_type:
                continue
            if score < self.min_amount20:
                continue
            out.append((symbol, score))
        out.sort(key=lambda x: -x[1])
        if self.top_n:
            out = out[: int(self.top_n)]
        return [_member_from_meta(s, meta.get(s)) for s, _ in out]


def register_universe_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="universe", name="static_list", version=1, factory=StaticListUniverse,
        params_schema={"symbols": {"type": "list", "required": True}},
        description="显式标的清单",
    ))
    registry.register(ModuleSpec(
        slot="universe", name="category_filter", version=1, factory=CategoryFilterUniverse,
        params_schema={
            "levels": {"type": "dict"},
            "asset_type": {"type": "string", "default": "all", "choices": ["all", "stock", "etf"]},
            "exclude_st": {"type": "boolean", "default": False},
            "enabled_only": {"type": "boolean", "default": True},
        },
        description="三级类目 + enabled + asset_type 过滤",
    ))
    registry.register(ModuleSpec(
        slot="universe", name="liquidity_filter", version=1, factory=LiquidityFilterUniverse,
        params_schema={
            "min_amount20": {"type": "number", "default": 1e8, "min": 0},
            "top_n": {"type": "integer", "min": 1},
            "asset_type": {"type": "string", "default": "all", "choices": ["all", "stock", "etf"]},
            "enabled_only": {"type": "boolean", "default": True},
        },
        description="近 20 日成交额 ≥ 阈值（可选 top_n）",
    ))
