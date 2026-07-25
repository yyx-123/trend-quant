"""Progress-callback tests for the rule backtest engine and service.

Covers:
- Engine invokes ``progress_callback(day_no, total_days)`` once per bar,
  monotonically, ending exactly at ``(total, total)``.
- Service aggregates progress across multiple strategies
  (total = bars x strategy count) and stays backward compatible when no
  callback is provided.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from rule_backtest import BacktestExecutionConfig, RuleBacktestRequest, SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.service import RuleBacktestService


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


def flat_strategy(strategy_id: str) -> dict:
    """A strategy that never enters (entry condition always false)."""
    return {
        "schema_version": 1,
        "id": strategy_id,
        "name": strategy_id,
        "trade_mode": "single_symbol_all_in",
        "entry": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "type": "condition",
                    "left": {"type": "literal", "value": 0},
                    "operator": ">=",
                    "right": {"type": "literal", "value": 1},
                }
            ],
        },
        "exit": {
            "type": "group",
            "combinator": "any",
            "children": [
                {
                    "type": "condition",
                    "left": {"type": "literal", "value": 0},
                    "operator": ">=",
                    "right": {"type": "literal", "value": 1},
                }
            ],
        },
    }


class FakeMarketStore:
    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars

    def load_history(self, symbol: str) -> pd.DataFrame:
        return self._bars

    def list_stored_symbols(self) -> list[str]:
        return ["TEST"]


class EngineProgressTest(unittest.TestCase):
    def test_engine_reports_progress_once_per_bar(self) -> None:
        closes = [10.0] * 30
        bars = make_bars(closes)
        calls: list[tuple[int, int]] = []

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=flat_strategy("never_entry"),
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
                progress_callback=lambda cur, total: calls.append((cur, total)),
            )
        )

        self.assertEqual("ok", result["status"])
        expected = [(i + 1, len(closes)) for i in range(len(closes))]
        self.assertEqual(expected, calls)

    def test_engine_progress_respects_date_filter(self) -> None:
        closes = [10.0] * 30
        bars = make_bars(closes)
        calls: list[tuple[int, int]] = []

        SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=flat_strategy("never_entry"),
                symbol="TEST",
                bars=bars,
                start_date=date(2026, 1, 11),
                end_date=date(2026, 1, 20),
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
                progress_callback=lambda cur, total: calls.append((cur, total)),
            )
        )

        self.assertEqual(10, len(calls))
        self.assertEqual((10, 10), calls[-1])

    def test_engine_works_without_callback(self) -> None:
        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=flat_strategy("never_entry"),
                symbol="TEST",
                bars=make_bars([10.0] * 5),
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )
        self.assertEqual("ok", result["status"])


class ServiceProgressTest(unittest.TestCase):
    def _make_service(self, tmp: str, bars: pd.DataFrame, strategy_ids: list[str]) -> RuleBacktestService:
        loader = StrategyLoader(base_dir=Path(tmp) / "strategies")
        for sid in strategy_ids:
            loader.save(flat_strategy(sid))
        return RuleBacktestService(strategy_loader=loader, market_store=FakeMarketStore(bars))

    def test_service_aggregates_progress_across_strategies(self) -> None:
        n_bars = 20
        bars = make_bars([10.0] * n_bars)
        strategy_ids = ["strat_a", "strat_b"]
        with TemporaryDirectory() as tmp:
            service = self._make_service(tmp, bars, strategy_ids)
            calls: list[tuple[int, int]] = []
            result = service.run(
                {
                    "strategy_ids": strategy_ids,
                    "symbol": "TEST",
                    "initial_capital": 100000.0,
                    "slippage": 0.0,
                    "fee_rate": 0.0,
                    "fee_min": 0.0,
                },
                progress_callback=lambda cur, total: calls.append((cur, total)),
            )

        total_expected = n_bars * len(strategy_ids)
        self.assertEqual("ok", result["status"])
        self.assertEqual(2, len(result["results"]))
        # First call reports the real total up front (0, N) before any bar runs.
        self.assertEqual((0, total_expected), calls[0])
        bar_calls = calls[1:]
        self.assertEqual(total_expected, len(bar_calls))
        self.assertEqual((total_expected, total_expected), bar_calls[-1])
        # Progress is globally monotonic and stays within bounds.
        currents = [c for c, _ in bar_calls]
        self.assertEqual(sorted(currents), currents)
        self.assertTrue(all(1 <= c <= total_expected for c in currents))
        self.assertTrue(all(t == total_expected for _, t in bar_calls))
        # First strategy occupies the first half, second the second half.
        self.assertTrue(all(c <= n_bars for c in currents[:n_bars]))
        self.assertTrue(all(c > n_bars for c in currents[n_bars:]))

    def test_service_without_callback_matches_legacy_behavior(self) -> None:
        bars = make_bars([10.0] * 20)
        with TemporaryDirectory() as tmp:
            service = self._make_service(tmp, bars, ["strat_a"])
            result = service.run(
                {
                    "strategy_ids": ["strat_a"],
                    "symbol": "TEST",
                    "initial_capital": 100000.0,
                    "slippage": 0.0,
                    "fee_rate": 0.0,
                    "fee_min": 0.0,
                }
            )
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, len(result["results"]))


if __name__ == "__main__":
    unittest.main()
