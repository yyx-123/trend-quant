"""Position sizing (仓位管理) abstractions for the rule backtest engine.

A PositionSizer decides how much capital to commit when an entry signal
fires. The engine keeps final authority over affordability, lot alignment
and fee validation; a sizer only produces a *target* quantity plus
metadata (flags / note) that is surfaced in trade records and the UI.

Degradation policy (统一降级机制): whenever a sizer cannot compute its
theoretical value (e.g. Kelly fraction <= 0, no usable ATR), it falls back
to ``fallback_pct`` of equity instead of skipping the buy, and marks the
decision with a flag. Mechanical failures (cannot afford a single lot,
or the target rounds below one lot) are *not* degradations — the engine
records them in ``skipped_buys`` with an explicit reason.

Adding a new sizer = subclass PositionSizer + register in
``sizing.registry.SIZER_REGISTRY``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from rule_backtest.models import BacktestExecutionConfig
from rule_backtest.registry import ParamSpec

#: Unified degraded-position parameter shared by every sizer.
FALLBACK_PCT_PARAM = ParamSpec(type="float", required=False, default=0.10, min_value=0.0)

#: Skip reasons recorded in result["skipped_buys"].
SKIP_INSUFFICIENT_CASH = "insufficient_cash"
SKIP_TARGET_BELOW_LOT = "sizer_target_below_lot"
#: A sizer explicitly returned action="skip" (none of the built-in sizers do).
SKIP_SIZER = "sizer_skip"

#: Flags that indicate a degraded (fallback) decision — the engine logs a
#: warning and the UI highlights these buys. Other flags (e.g.
#: ``atr_fallback_prev_day``, ``risk_budget_unconstrained``) are info-only.
DEGRADED_FLAGS = frozenset({
    "kelly_floor_applied",
    "atr_unavailable_fallback",
})

#: Info-only flags (normal resolution paths, surfaced without highlighting).
INFO_FLAGS = frozenset({
    "atr_fallback_prev_day",
    "risk_budget_unconstrained",
})


class AtrSource(Protocol):
    """ATR lookup bound by the engine to the current day.

    ``lookback=0`` is the current day; ``lookback=N`` walks N bars back.
    Returns None for warmup gaps and out-of-range indexes
    (``ValueResolver._value_at`` returns None for idx < 0).
    """

    def __call__(self, period: int, lookback: int = 0) -> float | None: ...


@dataclass(slots=True)
class SizingContext:
    """Everything a sizer needs at a buy point."""

    cash: float
    # At a buy point there is never an open position, so equity == cash.
    # Both fields are kept to leave room for future additive sizers.
    equity: float
    reference_price: float  # day close
    exec_price: float  # reference_price with slippage applied
    atr_at: AtrSource
    # Closed SELL trades of this run (contain pnl / qty / avg_cost).
    closed_trades: list[dict]
    execution: BacktestExecutionConfig
    # Number of bars up to and including today (bounds ATR lookback).
    history_bars: int = 0
    # Engine-computed max affordable qty (fees + lot aligned). Sizers use
    # this as the exact "full buy" reference instead of re-estimating.
    affordable_qty: int = 0


@dataclass(slots=True)
class SizingDecision:
    action: str = "buy"  # "buy" | "skip"
    # Target quantity, explicitly floored to int by the sizer; NOT lot
    # aligned — lot alignment and fee validation are the engine's job.
    target_qty: int = 0
    # Target investment as a fraction of equity (display only).
    position_pct: float = 0.0
    flags: list[str] = field(default_factory=list)
    note: str = ""


class PositionSizer(ABC):
    sizer_type: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init__(self, fallback_pct: float = 0.10) -> None:
        fallback_pct = float(fallback_pct)
        if not 0.0 < fallback_pct <= 1.0:
            raise ValueError(f"fallback_pct must be in (0, 1], got {fallback_pct}")
        self.fallback_pct = fallback_pct
        # Attached by sizing.registry.build_sizer for trade annotations.
        self.strategy_id: str = ""
        self.strategy_name: str = ""

    @abstractmethod
    def decide(self, ctx: SizingContext) -> SizingDecision:
        """Return the sizing decision for one entry signal."""

    @classmethod
    @abstractmethod
    def param_specs(cls) -> dict[str, ParamSpec]:
        """Parameter schema, exposed via /api/meta to drive the UI form."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _per_share_cost(ctx: SizingContext) -> float:
        """Estimated per-share cost, aligned with engine._max_buy_qty
        (exec_price + exec_price * fee_rate; fee_min is not pre-deducted —
        a slight overestimate here is harmless because the engine clamps
        the final qty to what is actually affordable)."""
        return ctx.exec_price + ctx.exec_price * ctx.execution.fee_rate

    def _qty_for_amount(self, ctx: SizingContext, amount: float) -> int:
        per_share = self._per_share_cost(ctx)
        if per_share <= 0 or amount <= 0:
            return 0
        return int(amount // per_share)

    def _buy_decision(
        self,
        ctx: SizingContext,
        target_qty: int,
        flags: list[str] | None = None,
        note: str = "",
    ) -> SizingDecision:
        target_qty = max(int(target_qty), 0)
        position_pct = 0.0
        if ctx.equity > 0 and target_qty > 0:
            position_pct = min(target_qty * self._per_share_cost(ctx) / ctx.equity, 1.0)
        return SizingDecision(
            action="buy",
            target_qty=target_qty,
            position_pct=position_pct,
            flags=list(flags or []),
            note=note,
        )

    def _fallback_decision(self, ctx: SizingContext, flag: str, note: str) -> SizingDecision:
        """Unified degradation path: buy fallback_pct of equity + flag."""
        qty = self._qty_for_amount(ctx, ctx.equity * self.fallback_pct)
        return self._buy_decision(ctx, qty, flags=[flag], note=note)
