"""Engine-level tests for position sizing integration:

- partial buys leave cash idle (counted in equity, earning nothing)
- skipped buys are recorded with explicit reasons
- Kelly consumes this run's closed trades
- SELL trades carry avg_cost (Kelly's net-return basis)
- sizing flags propagate to BOTH buy_points payloads + skipped_buy_points
- sizer=None keeps legacy all-in behavior
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)
from rule_backtest.sizing import FixedPctSizer, KellySizer, RiskBudgetSizer


def make_bars(closes: list[float]) -> pd.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            {
                "date": start + timedelta(days=idx),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000 + idx,
                "amount": close * (1000 + idx),
            }
        )
    return pd.DataFrame(rows)


LITERAL_ENTRY = {
    "type": "group",
    "combinator": "all",
    "children": [{"left": {"type": "literal", "value": 1}, "operator": ">=", "right": {"type": "literal", "value": 0}}],
}
NEVER_EXIT = {
    "type": "group",
    "combinator": "any",
    "children": [{"left": {"type": "literal", "value": 0}, "operator": ">=", "right": {"type": "literal", "value": 1}}],
}


def make_strategy(entry: dict | None = None, exit_close_lte: float | None = None) -> dict:
    if exit_close_lte is not None:
        exit_group = {
            "type": "group",
            "combinator": "any",
            "children": [
                {"left": {"type": "price", "field": "close"}, "operator": "<=", "right": {"type": "literal", "value": exit_close_lte}}
            ],
        }
    else:
        exit_group = NEVER_EXIT
    return {"id": "sizing_test", "entry": entry or LITERAL_ENTRY, "exit": exit_group}


def run_engine(bars, sizer=None, strategy=None, initial_capital=100_000.0):
    return SingleSymbolAllInBacktestEngine().run(
        RuleBacktestRequest(
            strategy=strategy or make_strategy(),
            symbol="TEST",
            bars=bars,
            execution=BacktestExecutionConfig(
                initial_capital=initial_capital  # 默认费率（系统不提供零成本场景）
            ),
            sizer=sizer,
        )
    )


class TestFixedPctIntegration:
    def test_partial_buy_leaves_cash_idle(self):
        result = run_engine(make_bars([10.0, 10.0, 10.0]), sizer=FixedPctSizer(pct=0.5))
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        assert len(buys) == 1
        # 默认费率（零值归一）：exec=10.02，target=floor(50000/10.02/100)*100=4900
        assert buys[0]["qty"] == 4900
        sizing = buys[0]["sizing"]
        assert sizing["sizer_type"] == "fixed_pct"
        # position_pct 含费用口径：(4900×10.02+5)/100000
        assert sizing["position_pct"] == pytest.approx(0.49103)
        assert sizing["flags"] == []
        nav = result["daily_nav"][-1]
        # cash = 100000 − 4900×10.02 − max(gross×0.0000854, 5) = 50897
        assert nav["cash"] == pytest.approx(50_897.0)
        # equity = cash + 4900×10 = 99897（买入佣金与滑点成本已实现）
        assert nav["equity"] == pytest.approx(99_897.0)

    def test_no_sizer_keeps_all_in(self):
        result = run_engine(make_bars([10.0, 10.0, 10.0]), sizer=None)
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        # 默认费率（零值归一）：全仓 affordable = 9900（10000 会超出预算）
        assert buys[0]["qty"] == 9900
        assert "sizing" not in buys[0]
        assert result["sizer_id"] == ""


class TestKellyIntegration:
    def test_kelly_consumes_run_history_and_floors(self):
        # Round trips: buy@10 sell@8 (loss), buy@12 sell@8 (loss), buy@12.
        closes = [10.0, 8.0, 12.0, 8.0, 12.0]
        strategy = make_strategy(exit_close_lte=8.5)
        sizer = KellySizer()
        sizer.strategy_id = "k1"
        sizer.strategy_name = "凯利"
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)

        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        sells = [t for t in result["trades"] if t["side"] == "SELL"]
        # 默认费率（零值归一）：首笔全仓 affordable=9900；后两笔 Kelly 地板 10%
        # equity 下取整到百位均为 600（与零费率时相同）
        assert [t["qty"] for t in buys] == [9900, 600, 600]
        # first buy: no history -> all-in, unflagged
        assert buys[0]["sizing"]["flags"] == []
        # later buys: all-loss history -> Kelly floor (10% of equity)
        assert buys[1]["sizing"]["flags"] == ["kelly_floor_applied"]
        assert buys[2]["sizing"]["flags"] == ["kelly_floor_applied"]
        assert buys[1]["sizing"]["position_pct"] == pytest.approx(0.10, abs=0.02)  # lot rounding: 7200/80000
        # SELL trades carry avg_cost for the net-return basis
        # 默认费率：avg_cost=(gross+commission)/qty=99206.47/9900≈10.02086
        assert sells[0]["avg_cost"] == pytest.approx(99206.47 / 9900, rel=1e-6)
        # pnl = 卖出净额 − 买入净成本 ≈ 79034.85 − 99206.47
        assert sells[0]["pnl"] == pytest.approx(79034.85 - 99206.47, rel=1e-6)
        assert result["sizer_id"] == "k1"
        assert result["sizer_name"] == "凯利"

    def test_kelly_partial_after_profitable_history(self):
        # win +20% then loss -10%: p=0.5, b=2 -> f*=0.25
        closes = [10.0, 12.0, 8.0, 20.0]
        # sell high: exit when close <= 11 after buying at 10 -> sells at 8? No:
        # day2 close=12 no exit; need exit to trigger at 12 (win) then at 8 (loss).
        strategy = {
            "id": "win_then_loss",
            "entry": LITERAL_ENTRY,
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {"left": {"type": "price", "field": "close"}, "operator": ">=", "right": {"type": "literal", "value": 12.0}},
                    {"left": {"type": "price", "field": "close"}, "operator": "<=", "right": {"type": "literal", "value": 8.0}},
                ],
            },
        }
        sizer = KellySizer()
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        # day1 buy@10 all-in（默认费率 affordable=9900）；day2 sell@12 (+20%)；
        # day3 buy: history=1 win, no losses -> f*=p=1.0 -> all-in at 8
        # （现金≈119345.80，exec=8.016 → 14800）；day4 sell@20 (+150%)
        assert buys[0]["qty"] == 9900
        assert buys[1]["qty"] == 14800
        assert buys[1]["sizing"]["flags"] == []


class TestRiskBudgetIntegration:
    def test_risk_budget_partial_buy(self):
        # 25 flat bars (ATR=2 with high/low +-1), entry fires late via price threshold
        closes = [10.0] * 25 + [12.0, 12.0]
        strategy = make_strategy(
            entry={
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": {"type": "price", "field": "close"}, "operator": ">=", "right": {"type": "literal", "value": 11.0}}
                ],
            }
        )
        sizer = RiskBudgetSizer(mode="absolute", value=15000.0)
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        assert len(buys) == 1
        # ATR at the jump day = 2.05 (last-day TR=3 lifts it) -> 15000/3.075 = 4878 -> lot 4800
        assert buys[0]["qty"] == 4800
        assert buys[0]["sizing"]["flags"] == []
        assert result["skipped_buys"] == []

    def test_risk_budget_unconstrained_full_buy(self):
        closes = [10.0] * 25 + [12.0, 12.0]
        strategy = make_strategy(
            entry={
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": {"type": "price", "field": "close"}, "operator": ">=", "right": {"type": "literal", "value": 11.0}}
                ],
            }
        )
        sizer = RiskBudgetSizer(mode="absolute", value=60_000.0)  # target 20000 >= affordable
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        assert buys[0]["qty"] == 8300  # floor(100000/12/100)*100
        assert "risk_budget_unconstrained" in buys[0]["sizing"]["flags"]

    def test_early_buy_uses_early_atr(self):
        # The memoized ATR series yields values from bar 0 (no warmup mask for
        # atr in ValueResolver._value_at), so even a 3-bar history sizes via
        # ATR=2 instead of falling back. The atr_unavailable_fallback path is
        # covered by sizer-level unit tests with a synthetic ATR source.
        sizer = RiskBudgetSizer(mode="absolute", value=15000.0)
        result = run_engine(make_bars([10.0, 10.0, 10.0]), sizer=sizer)
        buys = [t for t in result["trades"] if t["side"] == "BUY"]
        assert buys[0]["qty"] == 5000  # 15000 / (1.5 * 2.0)
        assert buys[0]["sizing"]["flags"] == []


class TestSkippedBuys:
    def test_sizer_target_below_lot_is_recorded(self):
        # ATR=2.05 -> risk/share=3.075; budget 150 -> target 48 < lot 100 -> skip
        closes = [10.0] * 25 + [12.0]
        strategy = make_strategy(
            entry={
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": {"type": "price", "field": "close"}, "operator": ">=", "right": {"type": "literal", "value": 11.0}}
                ],
            }
        )
        sizer = RiskBudgetSizer(mode="absolute", value=150.0)
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)
        assert result["trades"] == []
        assert len(result["skipped_buys"]) == 1
        skip = result["skipped_buys"][0]
        assert skip["reason"] == "sizer_target_below_lot"
        assert skip["date"] == "2026-01-26"  # entry fires on the 26th bar
        # charts data link: skipped points in both payloads
        assert result["charts"]["skipped_buy_points"][0]["reason"] == "sizer_target_below_lot"
        assert result["charts"]["kline"]["skipped_buy_points"][0]["reason"] == "sizer_target_below_lot"

    def test_insufficient_cash_is_recorded(self):
        result = run_engine(make_bars([10.0, 10.0]), initial_capital=50.0)
        assert result["trades"] == []
        assert result["skipped_buys"][0]["reason"] == "insufficient_cash"

    def test_sizer_initiated_skip_has_own_reason(self):
        from rule_backtest.sizing.base import PositionSizer, SizingDecision

        class SkipSizer(PositionSizer):
            sizer_type = "skip_stub"

            def decide(self, ctx):
                return SizingDecision(action="skip", note="stub skip")

            @classmethod
            def param_specs(cls):
                return {}

        result = run_engine(make_bars([10.0, 10.0]), sizer=SkipSizer())
        assert result["trades"] == []
        assert result["skipped_buys"][0]["reason"] == "sizer_skip"
        assert result["skipped_buys"][0]["note"] == "stub skip"


class TestBuyPointFlags:
    def test_flags_propagate_to_both_buy_points_payloads(self):
        closes = [10.0, 8.0, 12.0]
        strategy = make_strategy(exit_close_lte=8.5)
        sizer = KellySizer()
        result = run_engine(make_bars(closes), sizer=sizer, strategy=strategy)
        flagged = [p for p in result["charts"]["buy_points"] if "kelly_floor_applied" in p["flags"]]
        assert len(flagged) == 1
        kline_flagged = [p for p in result["charts"]["kline"]["buy_points"] if "kelly_floor_applied" in p["flags"]]
        assert len(kline_flagged) == 1
