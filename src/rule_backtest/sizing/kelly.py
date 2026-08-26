from __future__ import annotations

from rule_backtest.registry import ParamSpec
from rule_backtest.sizing.base import (
    FALLBACK_PCT_PARAM,
    PositionSizer,
    SizingContext,
    SizingDecision,
)


class KellySizer(PositionSizer):
    """Optimal Kelly fraction based on this run's recent closed trades.

    Statistics are computed on per-trade NET RETURNS, not absolute amounts:
    ``ret_i = pnl_i / (qty_i * avg_cost_i)`` (avg_cost already includes buy
    fees, pnl is net, so the ratio is exactly the net return). Amount-based
    statistics would let later, larger positions dominate the payoff ratio
    and distort the Kelly fraction.

    - No history (first buy) -> all-in, per the confirmed requirement.
    - f* <= 0 -> unified degradation (fallback_pct), flag ``kelly_floor_applied``.
    - No losing samples -> b treated as infinite, f* = p.
    - f* is capped by max_pct.
    """

    sizer_type = "kelly"
    label = "凯利比"

    def __init__(
        self,
        lookback: int = 10,
        fraction: float = 1.0,
        max_pct: float = 1.0,
        fallback_pct: float = 0.10,
    ) -> None:
        super().__init__(fallback_pct=fallback_pct)
        lookback = int(lookback)
        if lookback < 1:
            raise ValueError(f"lookback must be >= 1, got {lookback}")
        fraction = float(fraction)
        if fraction <= 0:
            raise ValueError(f"fraction must be positive, got {fraction}")
        max_pct = float(max_pct)
        if not 0.0 < max_pct <= 1.0:
            raise ValueError(f"max_pct must be in (0, 1], got {max_pct}")
        self.lookback = lookback
        self.fraction = fraction
        self.max_pct = max_pct

    @classmethod
    def param_specs(cls) -> dict[str, ParamSpec]:
        return {
            "lookback": ParamSpec(type="int", required=False, default=10, min_value=1),
            "fraction": ParamSpec(type="float", required=False, default=1.0, min_value=0.0),
            "max_pct": ParamSpec(type="float", required=False, default=1.0, min_value=0.0),
            "fallback_pct": FALLBACK_PCT_PARAM,
        }

    def decide(self, ctx: SizingContext) -> SizingDecision:
        returns = [r for r in (self._net_return(t) for t in ctx.closed_trades) if r is not None]
        returns = returns[-self.lookback :]
        if not returns:
            qty = self._qty_for_amount(ctx, ctx.cash)
            return self._buy_decision(ctx, qty, note="首次买入，无历史交易，全仓")

        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]
        p = len(wins) / len(returns)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

        if not losses:
            # No losing sample: b -> infinity, f* = p (normal path).
            f_star = p
        elif avg_win <= 0:
            f_star = 0.0
        else:
            b = avg_win / avg_loss
            f_star = p - (1.0 - p) / b
        f_star *= self.fraction

        if f_star <= 0:
            return self._fallback_decision(
                ctx,
                "kelly_floor_applied",
                f"凯利比 f*={f_star:.1%} ≤ 0（近{len(returns)}笔），按降级仓位买入",
            )
        pct = min(f_star, self.max_pct)
        qty = self._qty_for_amount(ctx, ctx.equity * pct)
        capped = "（已封顶）" if f_star > self.max_pct else ""
        note = f"凯利比 f*={f_star:.1%}{capped}，胜率 {p:.0%}，近{len(returns)}笔"
        return self._buy_decision(ctx, qty, note=note)

    @staticmethod
    def _net_return(trade: dict) -> float | None:
        pnl = trade.get("pnl")
        qty = trade.get("qty") or 0
        avg_cost = trade.get("avg_cost") or 0.0
        if pnl is None or qty <= 0 or avg_cost <= 0:
            return None
        basis = float(qty) * float(avg_cost)
        if basis <= 0:
            return None
        return float(pnl) / basis
