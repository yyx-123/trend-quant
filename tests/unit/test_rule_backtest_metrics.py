"""Unit tests for rule_backtest.metrics — rule‑backtest compute functions."""

from __future__ import annotations

import pytest

from rule_backtest.metrics import (
    compute_annual_returns,
    compute_drawdown,
    compute_monthly_heatmap,
    compute_summary,
    annual_returns,
    monthly_returns,
)


def _make_nav(equities: list[float], start_date: str = "2025-01-02") -> list[dict]:
    """helper: create daily_nav list from equity values."""
    from datetime import date, timedelta

    base = date.fromisoformat(start_date)
    return [{"date": (base + timedelta(days=i)).isoformat(), "equity": e}
            for i, e in enumerate(equities)]


class TestComputeDrawdown:
    def test_no_drawdown(self) -> None:
        """rule_backtest.metrics.compute_drawdown returns list of dicts."""
        nav = _make_nav([100_000, 101_000, 102_000])
        dd = compute_drawdown(nav)
        assert len(dd) == 3
        for item in dd:
            assert item["drawdown"] == 0.0

    def test_with_drawdown(self) -> None:
        nav = _make_nav([100_000, 80_000, 90_000])
        dd = compute_drawdown(nav)
        assert dd[1]["drawdown"] == pytest.approx(-0.2, abs=0.01)


class TestComputeSummary:
    def test_returns_required_keys(self) -> None:
        nav = _make_nav([100_000, 110_000, 105_000])
        summary = compute_summary(nav, [], 0.0)
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe",
                     "sortino", "calmar", "win_rate", "profit_factor", "trade_count"):
            assert key in summary

    def test_no_trades_zero_win_rate(self) -> None:
        nav = _make_nav([100_000, 110_000])
        summary = compute_summary(nav, [], 0.0)
        assert summary["win_rate"] == 0.0
        assert summary["trade_count"] == 0


class TestAnnualReturns:
    def test_returns_year_list(self) -> None:
        nav = _make_nav([100_000, 120_000, 110_000] * 100)
        result = annual_returns(nav)
        assert len(result) >= 1
        assert "year" in result[0]


class TestMonthlyReturns:
    def test_returns_monthly_data(self) -> None:
        nav = _make_nav([100_000, 105_000] * 15)
        result = monthly_returns(nav)
        assert isinstance(result, list)


class TestComputeMonthlyHeatmap:
    def test_empty_nav_returns_empty_payload(self) -> None:
        result = compute_monthly_heatmap([])
        assert result["years"] == []
        assert len(result["months"]) == 12
        assert result["data"] == []

    def test_heatmap_shape_and_values(self) -> None:
        # 约 90 个自然日，横跨 3 个月度周期
        nav = _make_nav([100_000 + i * 500 for i in range(90)])
        result = compute_monthly_heatmap(nav)
        assert result["years"], "应至少包含一个年份"
        assert len(result["months"]) == 12
        assert result["data"], "应至少有一个月度收益点"
        for month_idx, year_idx, value in result["data"]:
            assert 0 <= month_idx <= 11
            assert 0 <= year_idx < len(result["years"])
            assert isinstance(value, float)
        # 单调上涨的净值，月度收益应为正
        assert all(point[2] > 0 for point in result["data"])


class TestComputeAnnualReturns:
    def test_empty_nav_returns_empty(self) -> None:
        assert compute_annual_returns([]) == []

    def test_enriched_fields_present(self) -> None:
        nav = _make_nav([100_000 + (i % 7) * 800 + i * 300 for i in range(120)])
        trades = [
            {"date": nav[10]["date"], "side": "SELL", "pnl": 1200.0},
            {"date": nav[40]["date"], "side": "SELL", "pnl": -300.0},
            {"date": nav[41]["date"], "side": "BUY", "pnl": 0.0},
        ]
        result = compute_annual_returns(nav, trades=trades)
        assert len(result) >= 1
        row = result[0]
        for key in ("year", "return", "sharpe", "max_drawdown", "calmar", "trade_count",
                    "win_rate", "profit_factor", "benchmark_return", "benchmark_sharpe",
                    "benchmark_max_drawdown", "benchmark_calmar"):
            assert key in row
        # 只有 SELL 计入交易统计
        assert row["trade_count"] == 2
        assert row["win_rate"] == pytest.approx(0.5)
        assert row["profit_factor"] == pytest.approx(4.0)
        # 未传基准时基准字段为 None
        assert row["benchmark_return"] is None
        assert row["benchmark_sharpe"] is None
        assert row["benchmark_max_drawdown"] is None
        assert row["benchmark_calmar"] is None

    def test_benchmark_fields_when_provided(self) -> None:
        nav = _make_nav([100_000 + i * 400 for i in range(90)])
        benchmark_nav = _make_nav([100_000 + i * 200 for i in range(90)])
        result = compute_annual_returns(nav, benchmark_daily_nav=benchmark_nav)
        assert len(result) >= 1
        row = result[0]
        assert row["benchmark_return"] is not None
        assert row["benchmark_sharpe"] is not None
        assert row["benchmark_max_drawdown"] is not None
        assert row["benchmark_calmar"] is not None
        # 策略净值增长快于基准，收益应高于基准
        assert row["return"] > row["benchmark_return"]

    def test_max_drawdown_and_calmar(self) -> None:
        # 先涨后跌：100 -> 120 -> 90，年内最大回撤 (90/120 - 1) = -25%
        nav = _make_nav([100_000, 120_000, 90_000])
        result = compute_annual_returns(nav)
        assert len(result) == 1
        row = result[0]
        assert row["max_drawdown"] == pytest.approx(-0.25)
        # 全年收益 -10%，卡玛比 = -0.10 / 0.25 = -0.4
        assert row["return"] == pytest.approx(-0.10)
        assert row["calmar"] == pytest.approx(-0.4)
        # 无回撤时卡玛比为 0
        up_only = compute_annual_returns(_make_nav([100_000, 110_000, 120_000]))
        assert up_only[0]["max_drawdown"] == pytest.approx(0.0)
        assert up_only[0]["calmar"] == pytest.approx(0.0)
