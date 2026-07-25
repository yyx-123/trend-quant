"""Unit tests for slim_backtest_result (transport trimming of backtest results)."""

from __future__ import annotations

import unittest

from rule_backtest.service import slim_backtest_result


def _full_result() -> dict:
    per_strategy_heavy = {
        "daily_nav": [{"date": "2026-01-05", "equity": 100000.0}],
        "charts": {"kline": {"dates": ["2026-01-05"], "candles": [[1, 2, 3, 4]]}},
        "drawdown": [{"date": "2026-01-05", "drawdown": 0.0}],
        "condition_trace": [{"date": "2026-01-05", "side": "ENTRY"}],
        "benchmark": {"series": []},
        "benchmark_summary": {"return": 0.05},
        "monthly_returns": [{"month": "2026-01", "return": 0.1}],
        "debug_log": [{"date": "2026-01-05"}],
    }
    strategy = {
        "run_id": "r1",
        "status": "ok",
        "strategy_id": "s1",
        "strategy_name": "策略一",
        "sizer_id": "",
        "sizer_name": "",
        "symbol": "TEST",
        "start_date": "2026-01-01",
        "end_date": "2026-02-01",
        "initial_capital": 100000.0,
        "final_equity": 110000.0,
        "summary": {"return": 0.1},
        "trades": [{"date": "2026-01-05", "side": "BUY", "exec_price": 12.3}],
        "skipped_buys": [],
        "annual_returns": [{"year": 2026, "return": 0.1}],
        "monthly_heatmap": {"2026-01": 0.1},
        **per_strategy_heavy,
    }
    return {
        "results": [strategy],
        "benchmark_summary": {"return": 0.05},
        "multi_kline": [{"strategy_id": "s1", "buy_points": [], "sell_points": [], "skipped_buy_points": []}],
        "status": "ok",
        "run_id": "r1",
        "strategy_id": "s1",
        "sizer_id": "",
        "sizer_name": "",
        "symbol": "TEST",
        "start_date": "2026-01-01",
        "end_date": "2026-02-01",
        "initial_capital": 100000.0,
        "final_equity": 110000.0,
        "summary": {"return": 0.1},
        "trades": [{"date": "2026-01-05", "side": "BUY", "exec_price": 12.3}],
        "skipped_buys": [],
        "annual_returns": [{"year": 2026, "return": 0.1}],
        "monthly_heatmap": {"2026-01": 0.1},
        "debug_log": [],
        # Top-level heavy backward-compat duplicates:
        "daily_nav": [{"date": "2026-01-05", "equity": 100000.0}],
        "charts": {"kline": {"dates": ["2026-01-05"], "candles": [[1, 2, 3, 4]]}},
        "drawdown": [{"date": "2026-01-05", "drawdown": 0.0}],
        "condition_trace": [{"date": "2026-01-05"}],
        "benchmark": {"series": []},
        "monthly_returns": [{"month": "2026-01", "return": 0.1}],
    }


class SlimBacktestResultTest(unittest.TestCase):
    def test_heavy_fields_are_dropped(self) -> None:
        slim = slim_backtest_result(_full_result())
        heavy = ("daily_nav", "charts", "drawdown", "condition_trace", "benchmark", "monthly_returns")
        for key in heavy:
            self.assertNotIn(key, slim)
            self.assertNotIn(key, slim["results"][0])
        self.assertNotIn("debug_log", slim["results"][0])

    def test_frontend_fields_are_kept(self) -> None:
        slim = slim_backtest_result(_full_result())
        top_kept = (
            "results", "benchmark_summary", "multi_kline", "status", "run_id",
            "strategy_id", "sizer_id", "sizer_name", "symbol", "start_date", "end_date",
            "initial_capital", "final_equity", "summary", "trades", "skipped_buys",
            "annual_returns", "monthly_heatmap", "debug_log",
        )
        for key in top_kept:
            self.assertIn(key, slim)
        strategy = slim["results"][0]
        per_kept = (
            "run_id", "status", "strategy_id", "strategy_name", "sizer_id", "sizer_name",
            "symbol", "start_date", "end_date", "initial_capital", "final_equity",
            "summary", "trades", "skipped_buys", "annual_returns", "monthly_heatmap",
        )
        for key in per_kept:
            self.assertIn(key, strategy)
        # Trades keep their full detail (price, commission, etc.).
        self.assertEqual(12.3, strategy["trades"][0]["exec_price"])

    def test_input_is_not_mutated(self) -> None:
        full = _full_result()
        slim_backtest_result(full)
        self.assertIn("daily_nav", full)
        self.assertIn("charts", full["results"][0])

    def test_missing_keys_are_tolerated(self) -> None:
        slim = slim_backtest_result({"status": "ok", "results": [{"strategy_id": "s1"}]})
        self.assertEqual("ok", slim["status"])
        self.assertEqual([{"strategy_id": "s1"}], slim["results"])


if __name__ == "__main__":
    unittest.main()
