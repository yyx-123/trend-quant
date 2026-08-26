from __future__ import annotations

from rule_backtest.registry import ParamSpec
from rule_backtest.sizing.base import (
    FALLBACK_PCT_PARAM,
    PositionSizer,
    SizingContext,
    SizingDecision,
)


class FixedPctSizer(PositionSizer):
    """Invest a fixed percentage of available cash on every entry.

    pct = 1.0 reproduces the legacy all-in behavior.
    """

    sizer_type = "fixed_pct"
    label = "固定比例"

    def __init__(self, pct: float = 1.0, fallback_pct: float = 0.10) -> None:
        super().__init__(fallback_pct=fallback_pct)
        pct = float(pct)
        if not 0.0 < pct <= 1.0:
            raise ValueError(f"pct must be in (0, 1], got {pct}")
        self.pct = pct

    @classmethod
    def param_specs(cls) -> dict[str, ParamSpec]:
        return {
            "pct": ParamSpec(type="float", required=False, default=1.0, min_value=0.0),
            "fallback_pct": FALLBACK_PCT_PARAM,
        }

    def decide(self, ctx: SizingContext) -> SizingDecision:
        qty = self._qty_for_amount(ctx, ctx.cash * self.pct)
        return self._buy_decision(ctx, qty, note=f"固定比例 {self.pct:.0%}")
