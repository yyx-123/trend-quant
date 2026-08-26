from __future__ import annotations

from rule_backtest.registry import ParamSpec
from rule_backtest.sizing.base import (
    FALLBACK_PCT_PARAM,
    PositionSizer,
    SizingContext,
    SizingDecision,
)


class RiskBudgetSizer(PositionSizer):
    """Size the position so a theoretical hard stop loses at most the budget.

    Theoretical stop = exec_price - atr_mul * ATR(atr_period), used ONLY for
    sizing — it does not affect the strategy's actual exit conditions.
    Per-share risk = atr_mul * ATR; target_qty = risk_budget / risk_per_share.

    If even a full buy stays within the budget, the buy is unconstrained
    (info flag ``risk_budget_unconstrained``). ATR gaps are resolved by
    walking back to the most recent usable value (info flag
    ``atr_fallback_prev_day``); if no ATR exists all the way back to the
    series start, the unified degradation path applies
    (``atr_unavailable_fallback``).
    """

    sizer_type = "risk_budget"
    label = "风险预算"

    MODES = ("absolute", "equity_pct")

    def __init__(
        self,
        mode: str = "absolute",
        value: float = 10000.0,
        atr_period: int = 20,
        atr_mul: float = 1.5,
        fallback_pct: float = 0.10,
    ) -> None:
        super().__init__(fallback_pct=fallback_pct)
        mode = str(mode).strip().lower()
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}, got {mode!r}")
        value = float(value)
        if value <= 0:
            raise ValueError(f"value must be positive, got {value}")
        atr_period = int(atr_period)
        if atr_period < 1:
            raise ValueError(f"atr_period must be >= 1, got {atr_period}")
        atr_mul = float(atr_mul)
        if atr_mul <= 0:
            raise ValueError(f"atr_mul must be positive, got {atr_mul}")
        self.mode = mode
        self.value = value
        self.atr_period = atr_period
        self.atr_mul = atr_mul

    @classmethod
    def param_specs(cls) -> dict[str, ParamSpec]:
        return {
            "mode": ParamSpec(type="str", required=False, default="absolute"),
            "value": ParamSpec(type="float", required=False, default=10000.0, min_value=0.0),
            "atr_period": ParamSpec(type="int", required=False, default=20, min_value=1),
            "atr_mul": ParamSpec(type="float", required=False, default=1.5, min_value=0.0),
            "fallback_pct": FALLBACK_PCT_PARAM,
        }

    def decide(self, ctx: SizingContext) -> SizingDecision:
        atr, lookback_used = self._resolve_atr(ctx)
        if atr is None or atr <= 0:
            return self._fallback_decision(
                ctx,
                "atr_unavailable_fallback",
                "买入日及历史均无可用 ATR，无法计算理论止损，按降级仓位买入",
            )
        # Constructor guarantees value > 0 and atr_mul > 0, and the engine
        # only consults the sizer when cash > 0, so risk_budget and
        # risk_per_share are always positive here.
        risk_budget = self.value if self.mode == "absolute" else ctx.equity * self.value
        risk_per_share = self.atr_mul * atr  # exec_price - theoretical stop

        target_qty = int(risk_budget // risk_per_share)
        flags: list[str] = []
        if lookback_used > 0:
            flags.append("atr_fallback_prev_day")
        # Exact "full buy" reference: the engine's fee- and lot-aligned
        # affordable quantity (fall back to an estimate if unavailable).
        full_qty = ctx.affordable_qty or self._qty_for_amount(ctx, ctx.cash)
        if target_qty >= full_qty:
            # A full buy stays within the risk budget — not a degradation.
            flags.append("risk_budget_unconstrained")
        budget_desc = f"{self.value:,.0f}元" if self.mode == "absolute" else f"权益的{self.value:.1%}"
        note = f"风险预算 {budget_desc}，每股风险 {risk_per_share:.3f}（ATR×{self.atr_mul}）"
        return self._buy_decision(ctx, target_qty, flags=flags, note=note)

    def _resolve_atr(self, ctx: SizingContext) -> tuple[float | None, int]:
        """Most recent usable ATR, walking back at most to the series start."""
        for lookback in range(max(int(ctx.history_bars), 1)):
            value = ctx.atr_at(self.atr_period, lookback)
            if value is not None and value > 0:
                return value, lookback
        return None, 0
