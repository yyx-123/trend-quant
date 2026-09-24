"""`sizing` 仓位插槽（详设 §5.2.4）。

protocol SizingModule: size(ctx, candidate, stop_price) -> OrderIntent | None
- 输入含 stop_price（position_risk 槽预估的拟止损价）——等风险的数学
  依赖止损距离，这是 sizing 与 position_risk 唯一的接口接触点；
- 返回 None = 放弃本候选（如止损距离 ≤ 0）；sizing 不做资金池分配——
  够不够钱是引擎和 portfolio_risk 的事；
- equal_risk 打捞自旧 risk_budget（数量 = 风险预算额 ÷ 止损距离；
  整手与现金裁量归引擎）。
"""

from __future__ import annotations

from engine.models import OrderIntent
from portfolio.registry import REGISTRY, ModuleSpec
from portfolio.slots.signal import SignalEvent


class EqualRiskSizing:
    """等风险：数量 = (权益 × risk_budget_pct) ÷ (预估买入价 − 拟止损价)。

    亏到止损时正好亏权益的 risk_budget_pct（详设 §5.2.0 示例）。
    """

    def __init__(self, params: dict) -> None:
        self.risk_budget_pct = float(params.get("risk_budget_pct", 0.0075))

    def size(self, ctx, candidate: SignalEvent, stop_price: float | None) -> OrderIntent | None:
        price = ctx.panel.value(candidate.symbol, "close")
        if price is None or price <= 0:
            return None
        if stop_price is None or stop_price >= price:
            return None  # 止损距离 ≤ 0 → 放弃
        risk_budget = ctx.account.equity() * self.risk_budget_pct
        qty = risk_budget / (price - stop_price)
        if qty <= 0:
            return None
        return OrderIntent(
            symbol=candidate.symbol, decision_date=ctx.date,
            intent_type="quantity", value=qty, source="signal",
        )


class FixedPctSizing:
    """定额：equity × pct。"""

    def __init__(self, params: dict) -> None:
        self.pct = float(params.get("pct", 0.1))

    def size(self, ctx, candidate: SignalEvent, stop_price) -> OrderIntent | None:
        value = ctx.account.equity() * self.pct
        if value <= 0:
            return None
        return OrderIntent(symbol=candidate.symbol, decision_date=ctx.date,
                           intent_type="target_value", value=value, source="signal")


class FixedSlotsSizing:
    """槽位均分：equity ÷ slots。"""

    def __init__(self, params: dict) -> None:
        self.slots = int(params.get("slots", 10))

    def size(self, ctx, candidate: SignalEvent, stop_price) -> OrderIntent | None:
        if self.slots <= 0:
            return None
        value = ctx.account.equity() / self.slots
        return OrderIntent(symbol=candidate.symbol, decision_date=ctx.date,
                           intent_type="target_value", value=value, source="signal")


class AllInSizing:
    """全进（benchmark 用）：把全部现金押给本候选（引擎做整手/现金裁量）。"""

    def __init__(self, params: dict) -> None:
        pass

    def size(self, ctx, candidate: SignalEvent, stop_price) -> OrderIntent | None:
        return OrderIntent(symbol=candidate.symbol, decision_date=ctx.date,
                           intent_type="target_value", value=ctx.account.cash, source="signal")


class TargetWeightSizing:
    """目标权重（配置型策略）：value = equity × weight。

    params: weights={symbol: w}（显式表）或 mode="equal"（成员数均分）。
    """

    def __init__(self, params: dict) -> None:
        self.weights = {str(k).strip().upper(): float(v)
                        for k, v in (params.get("weights") or {}).items()}
        self.mode = str(params.get("mode") or ("equal" if not self.weights else "explicit"))
        self._members_count = int(params.get("members_count", 0))

    def size(self, ctx, candidate: SignalEvent, stop_price) -> OrderIntent | None:
        if self.mode == "equal":
            n = self._members_count or int(ctx.params.get("_members_count", 0))
            if n <= 0:
                return None
            weight = 1.0 / n
        else:
            weight = self.weights.get(candidate.symbol)
            if weight is None:
                return None
        value = ctx.account.equity() * float(weight)
        if value <= 0:
            return None
        return OrderIntent(symbol=candidate.symbol, decision_date=ctx.date,
                           intent_type="target_value", value=value, source="signal")


def register_sizing_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="sizing", name="equal_risk", version=1, factory=EqualRiskSizing,
        params_schema={"risk_budget_pct": {"type": "number", "default": 0.0075, "min": 0.0001, "max": 0.2}},
        description="等风险（风险预算 ÷ 止损距离）",
    ))
    registry.register(ModuleSpec(
        slot="sizing", name="fixed_pct", version=1, factory=FixedPctSizing,
        params_schema={"pct": {"type": "number", "default": 0.1, "min": 0.001, "max": 1.0}},
        description="权益定额比例",
    ))
    registry.register(ModuleSpec(
        slot="sizing", name="fixed_slots", version=1, factory=FixedSlotsSizing,
        params_schema={"slots": {"type": "integer", "default": 10, "min": 1, "max": 100}},
        description="槽位均分",
    ))
    registry.register(ModuleSpec(
        slot="sizing", name="all_in", version=1, factory=AllInSizing,
        params_schema={}, description="全进（benchmark 用）",
    ))
    registry.register(ModuleSpec(
        slot="sizing", name="target_weight", version=1, factory=TargetWeightSizing,
        params_schema={
            "weights": {"type": "dict"},
            # R3B-P3-4：mode 缺省行为（weights 空时 equal）此前只在工厂内
            # 隐式成立——schema 显式声明 default，参数域契约不再漂移
            "mode": {"type": "string", "choices": ["equal", "explicit"],
                     "default": "equal"},
            "members_count": {"type": "integer", "min": 1},
        },
        description="目标权重（显式表 / 成员均分）",
    ))
