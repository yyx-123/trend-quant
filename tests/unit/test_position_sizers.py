"""Unit tests for position sizers (fixed_pct / risk_budget / kelly) and the
sizer registry."""

from __future__ import annotations

import pytest

from rule_backtest.models import BacktestExecutionConfig
from rule_backtest.sizing import (
    FixedPctSizer,
    KellySizer,
    RiskBudgetSizer,
    SizingContext,
    build_sizer,
    sizer_types_payload,
    validate_position_strategy,
)


def make_ctx(
    cash: float = 100_000.0,
    price: float = 10.0,
    atr: float | None = 0.5,
    closed_trades: list[dict] | None = None,
    history_bars: int = 100,
    atr_gap_days: int = 0,
    affordable_qty: int = 0,
) -> SizingContext:
    """atr_gap_days: number of most recent days whose ATR is None (warmup gap)."""
    execution = BacktestExecutionConfig(lot_size=100)  # 默认费率（不提供零成本场景）

    def atr_at(period: int, lookback: int = 0) -> float | None:
        if lookback >= history_bars:
            return None  # walked past the series start
        if lookback < atr_gap_days:
            return None  # recent warmup gap
        return atr

    return SizingContext(
        cash=cash,
        equity=cash,
        reference_price=price,
        exec_price=price * 1.002,
        atr_at=atr_at,
        closed_trades=closed_trades or [],
        execution=execution,
        history_bars=history_bars,
        affordable_qty=affordable_qty,
    )


def make_trade(pnl: float, qty: int = 100, avg_cost: float = 10.0) -> dict:
    return {"side": "SELL", "pnl": pnl, "qty": qty, "avg_cost": avg_cost}


# ----------------------------------------------------------------------
# FixedPctSizer
# ----------------------------------------------------------------------
class TestFixedPctSizer:
    def test_full_pct_is_all_in(self):
        d = FixedPctSizer(pct=1.0).decide(make_ctx())
        # per-share 含佣金 = 10.02×(1+0.0000854) ≈ 10.020856
        # → floor(100000 / 10.020856) = 9979
        assert d.target_qty == 9979
        assert d.action == "buy"
        assert d.flags == []
        assert d.position_pct == pytest.approx(9979 * 10.020855708 / 100_000.0)

    def test_partial_pct(self):
        d = FixedPctSizer(pct=0.7).decide(make_ctx())
        assert d.target_qty == 6985  # 70000 // 10.020856
        assert d.position_pct == pytest.approx(0.7, abs=0.01)

    @pytest.mark.parametrize("bad", [0.0, -0.5, 1.5])
    def test_invalid_pct_rejected(self, bad):
        with pytest.raises(ValueError):
            FixedPctSizer(pct=bad)


# ----------------------------------------------------------------------
# RiskBudgetSizer
# ----------------------------------------------------------------------
class TestRiskBudgetSizer:
    def test_absolute_budget_constrained(self):
        # risk/share = 1.5 * 0.5 = 0.75 -> floor(3000 / 0.75) = 4000 < affordable
        d = RiskBudgetSizer(mode="absolute", value=3000.0).decide(make_ctx())
        assert d.target_qty == 4000
        assert "risk_budget_unconstrained" not in d.flags
        assert d.position_pct == pytest.approx(4000 * 10.020855708 / 100_000.0)

    def test_absolute_budget_unconstrained_means_full_buy(self):
        # floor(50000 / 0.75) = 66666 >= affordable 9980 -> full buy stays in budget
        d = RiskBudgetSizer(mode="absolute", value=50_000.0).decide(make_ctx())
        assert d.target_qty == 66666
        assert "risk_budget_unconstrained" in d.flags

    def test_equity_pct_mode(self):
        # budget = 1% * 100000 = 1000 -> floor(1000 / 0.75) = 1333
        d = RiskBudgetSizer(mode="equity_pct", value=0.01).decide(make_ctx())
        assert d.target_qty == 1333

    def test_unconstrained_uses_engine_affordable_qty(self):
        # target 7450/0.75 = 9933：≥ 引擎精确 affordable 9900（费用/整手对齐后），
        # 但 < 粗略每股估算 9980 —— 只有以引擎精确值为准才会打 unconstrained；
        # 若误用粗略估算则不达标（该场景对两种实现可鉴别）。
        ctx = make_ctx(affordable_qty=9900)
        d = RiskBudgetSizer(mode="absolute", value=7450.0).decide(ctx)
        assert d.target_qty == 9933
        assert "risk_budget_unconstrained" in d.flags

    def test_atr_lookback_uses_previous_day(self):
        d = RiskBudgetSizer(mode="absolute", value=3000.0).decide(make_ctx(atr_gap_days=2))
        assert d.target_qty == 4000  # resolved via lookback=2
        assert "atr_fallback_prev_day" in d.flags
        assert "atr_unavailable_fallback" not in d.flags

    def test_atr_unavailable_falls_back_to_fallback_pct(self):
        d = RiskBudgetSizer(mode="absolute", value=3000.0).decide(make_ctx(atr=None))
        assert "atr_unavailable_fallback" in d.flags
        # fallback 10% of equity: floor(10000 / 10.020856) = 997
        assert d.target_qty == 997

    def test_atr_gap_beyond_series_start_falls_back(self):
        # history has only 3 bars, all with ATR None
        d = RiskBudgetSizer().decide(make_ctx(atr=None, history_bars=3))
        assert "atr_unavailable_fallback" in d.flags

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            RiskBudgetSizer(mode="bogus")
        with pytest.raises(ValueError):
            RiskBudgetSizer(value=0)
        with pytest.raises(ValueError):
            RiskBudgetSizer(atr_period=0)
        with pytest.raises(ValueError):
            RiskBudgetSizer(atr_mul=0)


# ----------------------------------------------------------------------
# KellySizer
# ----------------------------------------------------------------------
class TestKellySizer:
    def test_first_trade_all_in(self):
        d = KellySizer().decide(make_ctx(closed_trades=[]))
        assert d.target_qty == 9979  # same as all-in（含佣金 per-share）
        assert d.flags == []

    def test_kelly_fraction_from_net_returns(self):
        # 3 wins +10%, 1 loss -5% -> p=0.75, b=2 -> f*=0.75-0.25/2=0.625
        trades = [make_trade(100.0)] * 3 + [make_trade(-50.0)]
        d = KellySizer().decide(make_ctx(closed_trades=trades))
        assert d.target_qty == 6236  # 62500 // 10.020856
        assert d.position_pct == pytest.approx(0.625, abs=0.01)
        assert d.flags == []

    def test_amount_scale_does_not_change_fraction(self):
        """Same returns at different money scales must give the same f*."""
        small = [make_trade(100.0, qty=100, avg_cost=10.0)] * 3 + [make_trade(-50.0, qty=100, avg_cost=10.0)]
        large = [make_trade(1000.0, qty=1000, avg_cost=10.0)] * 3 + [make_trade(-500.0, qty=1000, avg_cost=10.0)]
        mixed = [make_trade(100.0, qty=100, avg_cost=10.0), make_trade(-500.0, qty=1000, avg_cost=10.0),
                 make_trade(1000.0, qty=1000, avg_cost=10.0), make_trade(100.0, qty=100, avg_cost=10.0)]
        q_small = KellySizer().decide(make_ctx(closed_trades=small)).target_qty
        q_large = KellySizer().decide(make_ctx(closed_trades=large)).target_qty
        q_mixed = KellySizer().decide(make_ctx(closed_trades=mixed)).target_qty
        assert q_small == q_large == q_mixed

    def test_negative_kelly_floors_to_fallback_pct(self):
        # p=0.25, b=1 -> f*=0.25-0.75 = -0.5
        trades = [make_trade(50.0)] + [make_trade(-50.0)] * 3
        d = KellySizer().decide(make_ctx(closed_trades=trades))
        assert "kelly_floor_applied" in d.flags
        assert d.target_qty == 997  # 10% fallback（含佣金 per-share）

    def test_all_losses_floor(self):
        trades = [make_trade(-50.0)] * 4
        d = KellySizer().decide(make_ctx(closed_trades=trades))
        assert "kelly_floor_applied" in d.flags

    def test_no_losses_gives_full_kelly(self):
        trades = [make_trade(100.0)] * 3
        d = KellySizer().decide(make_ctx(closed_trades=trades))
        assert d.target_qty == 9979  # f* = p = 1.0 -> all-in（含佣金 per-share）
        assert d.flags == []

    def test_max_pct_caps(self):
        trades = [make_trade(100.0)] * 3
        d = KellySizer(max_pct=0.5).decide(make_ctx(closed_trades=trades))
        assert d.target_qty == 4989  # 50000 // 10.020856
        assert d.position_pct == pytest.approx(0.5, abs=0.01)

    def test_fraction_multiplier(self):
        trades = [make_trade(100.0)] * 3 + [make_trade(-50.0)]  # f* = 0.625
        d = KellySizer(fraction=0.5).decide(make_ctx(closed_trades=trades))
        assert d.target_qty == 3118  # 31250 // 10.020856

    def test_lookback_windows_history(self):
        # last-2 window [+10%, -5%]: p=0.5, b=2 -> f*=0.25
        trades = [make_trade(100.0), make_trade(100.0), make_trade(-50.0)]
        d2 = KellySizer(lookback=2).decide(make_ctx(closed_trades=trades))
        assert d2.target_qty == 2494  # 25000 // 10.020856
        # full window: p=2/3, b=2 -> f*=0.5
        d3 = KellySizer(lookback=3).decide(make_ctx(closed_trades=trades))
        assert d3.target_qty == 4989  # 50000 // 10.020856

    def test_trade_without_avg_cost_is_ignored(self):
        trades = [{"side": "SELL", "pnl": -50.0, "qty": 100}]  # no avg_cost
        d = KellySizer().decide(make_ctx(closed_trades=trades))
        assert d.target_qty == 9979  # treated as no history -> all-in（含佣金 per-share）
        assert d.flags == []

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            KellySizer(lookback=0)
        with pytest.raises(ValueError):
            KellySizer(fraction=0)
        with pytest.raises(ValueError):
            KellySizer(max_pct=1.5)


# ----------------------------------------------------------------------
# Registry: validation / build / meta payload
# ----------------------------------------------------------------------
class TestSizerRegistry:
    def test_validate_fills_defaults(self):
        result = validate_position_strategy({"id": "k1", "sizer_type": "kelly"})
        assert result.ok
        assert result.normalized["params"]["lookback"] == 10
        assert result.normalized["params"]["fallback_pct"] == 0.10

    def test_validate_coerces_types(self):
        result = validate_position_strategy(
            {"id": "k1", "sizer_type": "kelly", "params": {"lookback": "5", "fraction": 1}}
        )
        assert result.ok
        assert result.normalized["params"]["lookback"] == 5
        assert result.normalized["params"]["fraction"] == 1.0

    def test_validate_rejects_unknown_type(self):
        result = validate_position_strategy({"id": "x", "sizer_type": "martingale"})
        assert not result.ok
        assert any("unknown sizer_type" in e for e in result.errors)

    def test_validate_rejects_unknown_param(self):
        result = validate_position_strategy(
            {"id": "x", "sizer_type": "kelly", "params": {"lookbak": 5}}
        )
        assert not result.ok
        assert any("unknown param" in e for e in result.errors)

    def test_validate_rejects_constructor_errors(self):
        result = validate_position_strategy(
            {"id": "x", "sizer_type": "fixed_pct", "params": {"pct": 1.5}}
        )
        assert not result.ok

    def test_validate_requires_id(self):
        result = validate_position_strategy({"sizer_type": "kelly"})
        assert not result.ok

    def test_build_sizer_attaches_identity(self):
        payload = validate_position_strategy(
            {"id": "k1", "name": "凯利10", "sizer_type": "kelly", "params": {"lookback": 5}}
        ).normalized
        sizer = build_sizer(payload)
        assert isinstance(sizer, KellySizer)
        assert sizer.lookback == 5
        assert sizer.strategy_id == "k1"
        assert sizer.strategy_name == "凯利10"

    def test_sizer_types_payload_shape(self):
        payload = sizer_types_payload()
        by_type = {item["type"]: item for item in payload}
        assert set(by_type) == {"fixed_pct", "risk_budget", "kelly"}
        kelly_params = by_type["kelly"]["params"]
        assert kelly_params["lookback"]["default"] == 10
        assert "fallback_pct" in by_type["risk_budget"]["params"]
