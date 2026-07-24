from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import yaml

from core import indicators as core_ind
from data.storage.db import Database
from rule_backtest import BacktestExecutionConfig, RuleBacktestRequest, SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.models import PositionState
from rule_backtest.service import RuleBacktestService

SAMPLE_STRATEGY = {
    "schema_version": 1,
    "id": "close_above_sma20",
    "name": "收盘价上穿/跌破 SMA20",
    "trade_mode": "single_symbol_all_in",
    "description": "收盘价大于等于 SMA20 时买入，收盘价小于等于 SMA20 时卖出。",
    "entry": {
        "type": "group",
        "combinator": "all",
        "children": [
            {
                "id": "close_gte_sma20",
                "type": "condition",
                "left": {"type": "price", "field": "close"},
                "operator": ">=",
                "right": {"type": "indicator", "name": "sma", "params": {"field": "close", "period": 20}},
            }
        ],
    },
    "exit": {
        "type": "group",
        "combinator": "all",
        "children": [
            {
                "id": "close_lte_sma20",
                "type": "condition",
                "left": {"type": "price", "field": "close"},
                "operator": "<=",
                "right": {"type": "indicator", "name": "sma", "params": {"field": "close", "period": 20}},
            }
        ],
    },
}


def make_bars(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for idx, close in enumerate(closes):
        high = highs[idx] if highs is not None else close + 1
        low = lows[idx] if lows is not None else close - 1
        rows.append(
            {
                "date": start + timedelta(days=idx),
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000 + idx,
                "amount": close * (1000 + idx),
            }
        )
    return pd.DataFrame(rows)


def literal_entry_strategy(exit_rule: dict) -> dict:
    return {
        "id": "literal_entry",
        "entry": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "left": {"type": "literal", "value": 1},
                    "operator": ">=",
                    "right": {"type": "literal", "value": 0},
                }
            ],
        },
        "exit": {"type": "group", "combinator": "all", "children": [exit_rule]},
    }


def sma_cross_days(closes: list[float], period: int = 20) -> tuple[set[int], set[int]]:
    """Indices where close crosses above/below SMA(period), same semantics as
    the cross_above/cross_below operators."""
    sma = core_ind.sma(pd.Series(closes, dtype=float), period)
    golden: set[int] = set()
    death: set[int] = set()
    for i in range(1, len(closes)):
        prev_sma, cur_sma = sma.iloc[i - 1], sma.iloc[i]
        if pd.isna(prev_sma) or pd.isna(cur_sma):
            continue
        if closes[i - 1] <= prev_sma and closes[i] > cur_sma:
            golden.add(i)
        if closes[i - 1] >= prev_sma and closes[i] < cur_sma:
            death.add(i)
    return golden, death


def macd_cross_days(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[set[int], set[int]]:
    """Indices of DIF/DEA golden/death crosses, honoring the resolver's warmup
    mask (values are None while idx + 1 < max(fast, slow) + signal)."""
    out = core_ind.macd(
        pd.Series(closes, dtype=float),
        fast_period=fast,
        slow_period=slow,
        signal_period=signal,
        warmup=True,
    )
    dif, dea = out["dif"], out["dea"]
    min_rows = max(fast, slow) + signal
    golden: set[int] = set()
    death: set[int] = set()
    for i in range(1, len(closes)):
        if i < min_rows:  # prev day (i-1) would still be warmup-masked
            continue
        d_prev, e_prev, d_cur, e_cur = dif.iloc[i - 1], dea.iloc[i - 1], dif.iloc[i], dea.iloc[i]
        if any(pd.isna(v) for v in (d_prev, e_prev, d_cur, e_cur)):
            continue
        if d_prev <= e_prev and d_cur > e_cur:
            golden.add(i)
        if d_prev >= e_prev and d_cur < e_cur:
            death.add(i)
    return golden, death


class RuleBacktestEngineTest(unittest.TestCase):
    def test_exit_conditions_are_or_connected_even_for_legacy_all_strategy(self) -> None:
        bars = make_bars([100.0, 100.0])
        strategy = literal_entry_strategy(
            {
                "left": {"type": "literal", "value": 0},
                "operator": ">=",
                "right": {"type": "literal", "value": 1},
            }
        )
        strategy["exit"]["children"].append(
            {
                "left": {"type": "literal", "value": 1},
                "operator": ">=",
                "right": {"type": "literal", "value": 1},
            }
        )

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

        self.assertEqual(["BUY", "SELL"], [trade["side"] for trade in result["trades"]])
        self.assertEqual("2026-01-02", result["trades"][1]["date"])

    def test_sma20_entry_and_exit_strategy(self) -> None:
        bars = make_bars(([10.0] * 20) + ([12.0] * 5) + ([8.0] * 5))
        strategy = {
            "id": "close_vs_sma20",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
        }

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

        sides = [trade["side"] for trade in result["trades"]]
        self.assertEqual(["BUY", "SELL"], sides)
        self.assertEqual("2026-01-20", result["trades"][0]["date"])
        self.assertEqual("2026-01-26", result["trades"][1]["date"])
        self.assertLess(result["summary"]["max_drawdown"], 0)
        self.assertEqual(len(result["debug_log"]), len(bars))
        kline = result["charts"]["kline"]
        self.assertEqual(len(bars), len(kline["dates"]))
        self.assertEqual(len(bars), len(kline["candles"]))
        self.assertIn("20", kline["ma"])
        self.assertEqual(1, len(kline["buy_points"]))
        self.assertEqual(1, len(kline["sell_points"]))

    def test_indicator_warmup_uses_history_before_backtest_start(self) -> None:
        bars = make_bars(([10.0] * 40) + [12.0, 12.0, 12.0])
        strategy = {
            "id": "close_vs_sma40",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 40},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "literal", "value": 0},
                        "operator": ">=",
                        "right": {"type": "literal", "value": 1},
                    }
                ],
            },
        }

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                start_date=date(2026, 2, 10),
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

        self.assertEqual("2026-02-10", result["start_date"])
        self.assertEqual("2026-02-10", result["trades"][0]["date"])
        self.assertEqual(3, len(result["daily_nav"]))
        self.assertEqual(3, len(result["charts"]["kline"]["dates"]))

    def test_hard_stop_sells_at_stop_price_with_cost_fields(self) -> None:
        bars = make_bars([100.0, 90.0], highs=[101.0, 91.0], lows=[99.0, 89.0])
        strategy = literal_entry_strategy(
            {
                "left": {"type": "price", "field": "close"},
                "operator": "<=",
                "right": {
                    "type": "state_value",
                    "name": "hard_stop",
                    "params": {"atr_period": 1, "atr_mul": 1.0},
                },
            }
        )

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

        sell = result["trades"][1]
        self.assertEqual("hard_stop", sell["reason"])
        self.assertEqual(98.0, sell["reference_price"])
        self.assertEqual(98.0, sell["exec_price"])
        self.assertIn("commission", sell)
        self.assertIn("stamp_tax", sell)
        self.assertEqual(0.0, sell["stamp_tax"])

    def test_chandelier_stop_sells_at_stop_price(self) -> None:
        bars = make_bars(
            [100.0, 109.0, 99.0],
            highs=[101.0, 110.0, 101.0],
            lows=[99.0, 109.0, 100.0],
        )
        strategy = literal_entry_strategy(
            {
                "left": {"type": "price", "field": "close"},
                "operator": "<=",
                "right": {
                    "type": "state_value",
                    "name": "chandelier_stop",
                    "params": {"atr_period": 1, "atr_mul": 1.0},
                },
            }
        )

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

        sell = result["trades"][1]
        self.assertEqual("chandelier_stop", sell["reason"])
        self.assertEqual(101.0, sell["reference_price"])
        self.assertEqual(101.0, sell["exec_price"])

    def test_stock_sell_charges_stamp_tax_and_slippage(self) -> None:
        bars = make_bars([100.0, 90.0], highs=[101.0, 91.0], lows=[99.0, 89.0])
        strategy = literal_entry_strategy(
            {
                "left": {"type": "price", "field": "close"},
                "operator": "<=",
                "right": {
                    "type": "state_value",
                    "name": "hard_stop",
                    "params": {"atr_period": 1, "atr_mul": 1.0},
                },
            }
        )

        result = SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="STOCK",
                bars=bars,
                execution=BacktestExecutionConfig(
                    initial_capital=100000.0,
                    slippage=0.01,
                    fee_rate=0.001,
                    fee_min=5.0,
                    instrument_type="stock",
                    stock_stamp_tax_rate=0.001,
                ),
            )
        )

        buy, sell = result["trades"]
        self.assertAlmostEqual(101.0, buy["exec_price"])
        self.assertAlmostEqual(98.01, sell["exec_price"])
        self.assertGreater(sell["stamp_tax"], 0)
        self.assertGreater(result["summary"]["total_commission"], 0)
        self.assertGreater(result["summary"]["total_stamp_tax"], 0)
        self.assertGreater(result["summary"]["total_trading_cost"], 0)


class CrossOperatorEngineTest(unittest.TestCase):
    def _run(self, strategy: dict, bars: pd.DataFrame) -> dict:
        return SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

    @staticmethod
    def _trade_day_indices(result: dict, bars: pd.DataFrame) -> dict[str, list[int]]:
        day_to_idx = {str(day): idx for idx, day in enumerate(bars["date"])}
        out: dict[str, list[int]] = {"BUY": [], "SELL": []}
        for trade in result["trades"]:
            out[trade["side"]].append(day_to_idx[trade["date"]])
        return out

    def test_cross_above_buys_only_on_fresh_cross(self) -> None:
        # 用户在 MACD 金叉上遇到的场景（用 close vs SMA20 复现同样机制）：
        # 上穿买入后，一次"止损式"卖出发生在 close 仍高于 SMA20 的回撤日；
        # 次日 close 依然高于 SMA20，但不能再触发买入——只有重新下穿再上穿才行。
        closes = (
            [10.0] * 25
            + [10.5, 11.5, 12.5]
            + [12.5] * 4
            + [11.0]  # idx 32: 止损式回撤，但仍在 SMA20 之上
            + [12.5] * 2
            + [10.0] * 3  # idx 35-37: 真正跌破 SMA20
            + [12.0, 12.0]  # idx 38: 重新上穿
        )
        bars = make_bars(closes)
        strategy = {
            "id": "close_cross_sma20",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "cross_above",
                        "right": {"type": "indicator", "name": "sma", "params": {"field": "close", "period": 20}},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 11.2},
                    }
                ],
            },
        }

        result = self._run(strategy, bars)
        indices = self._trade_day_indices(result, bars)
        golden, _ = sma_cross_days(closes)

        self.assertEqual(sorted(golden), indices["BUY"])
        self.assertEqual([32], indices["SELL"])
        self.assertNotIn(33, indices["BUY"])  # 回撤次日 close 仍在 SMA20 上，但非新交叉
        self.assertEqual(2, len(indices["BUY"]))

    def test_macd_golden_cross_entry_and_death_cross_exit(self) -> None:
        closes = (
            [40.0 - 0.2 * i for i in range(45)]  # 长期下跌 → DIF < DEA
            + [31.2 + 0.6 * i for i in range(1, 31)]  # 反弹 → 金叉
            + [49.2 - 0.8 * i for i in range(1, 31)]  # 转跌 → 死叉
        )
        bars = make_bars(closes)
        macd_spec_left = {"type": "indicator", "name": "macd_line", "params": {"field": "close"}}
        macd_spec_right = {"type": "indicator", "name": "macd_signal", "params": {"field": "close"}}
        strategy = {
            "id": "macd_cross",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": macd_spec_left, "operator": "cross_above", "right": macd_spec_right}
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {"left": macd_spec_left, "operator": "cross_below", "right": macd_spec_right}
                ],
            },
        }

        result = self._run(strategy, bars)
        indices = self._trade_day_indices(result, bars)
        golden, death = macd_cross_days(closes)

        self.assertGreaterEqual(len(indices["BUY"]), 1)
        self.assertEqual(min(golden), indices["BUY"][0])
        self.assertLessEqual(set(indices["BUY"]), golden)
        self.assertLessEqual(set(indices["SELL"]), death)
        if indices["SELL"]:
            self.assertGreater(indices["SELL"][0], indices["BUY"][0])

    def test_macd_cross_not_triggered_during_warmup(self) -> None:
        # 30 根 K 线 < MACD warmup（26+9=35），DIF/DEA 全程为 None，绝不触发。
        bars = make_bars([40.0 - 0.5 * i for i in range(15)] + [33.0 + 0.5 * i for i in range(1, 16)])
        macd_spec_left = {"type": "indicator", "name": "macd_line", "params": {"field": "close"}}
        macd_spec_right = {"type": "indicator", "name": "macd_signal", "params": {"field": "close"}}
        strategy = {
            "id": "macd_cross_warmup",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": macd_spec_left, "operator": "cross_above", "right": macd_spec_right}
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {"left": macd_spec_left, "operator": "cross_below", "right": macd_spec_right}
                ],
            },
        }

        result = self._run(strategy, bars)
        self.assertEqual([], result["trades"])

    def test_cross_requires_previous_day(self) -> None:
        # 首日没有前一日数据，即使当日 close 已高于阈值也不能算"上穿"。
        bars = make_bars([3.0, 3.0])
        strategy = {
            "id": "cross_first_day",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "cross_above",
                        "right": {"type": "literal", "value": 2.0},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "left": {"type": "literal", "value": 0},
                        "operator": ">=",
                        "right": {"type": "literal", "value": 1},
                    }
                ],
            },
        }

        result = self._run(strategy, bars)
        self.assertEqual([], result["trades"])

    def test_price_literal_cross_roundtrip_and_trace(self) -> None:
        bars = make_bars([1.0, 3.0, 0.5])
        strategy = {
            "id": "price_literal_cross",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "cross_above",
                        "right": {"type": "literal", "value": 2.0},
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "cross_below",
                        "right": {"type": "literal", "value": 1.0},
                    }
                ],
            },
        }

        result = self._run(strategy, bars)
        self.assertEqual(["BUY", "SELL"], [trade["side"] for trade in result["trades"]])
        self.assertEqual("2026-01-02", result["trades"][0]["date"])
        self.assertEqual("2026-01-03", result["trades"][1]["date"])

        buy_traces = [
            row for row in result["condition_trace"] if row["side"] == "ENTRY" and row["date"] == "2026-01-02"
        ]
        self.assertEqual(1, len(buy_traces))
        self.assertEqual(1.0, buy_traces[0]["left_prev_value"])
        self.assertEqual(2.0, buy_traces[0]["right_prev_value"])
        self.assertTrue(buy_traces[0]["passed"])


class RuleBacktestLoaderServiceTest(unittest.TestCase):
    def test_loader_reads_sample_strategy(self) -> None:
        with TemporaryDirectory() as tmp:
            Path(tmp, "close_above_sma20.yaml").write_text(
                yaml.safe_dump(SAMPLE_STRATEGY, allow_unicode=True), encoding="utf-8"
            )
            loader = StrategyLoader(base_dir=tmp)
            strategies = loader.list_strategies()
            ids = {item["id"] for item in strategies}
            self.assertIn("close_above_sma20", ids)

            strategy = loader.load("close_above_sma20")
            self.assertEqual("single_symbol_all_in", strategy["trade_mode"])
            self.assertEqual("sma", strategy["entry"]["children"][0]["right"]["name"])

    def test_loader_saves_new_strategy_yaml(self) -> None:
        strategy = {
            "schema_version": 1,
            "id": "test_created_strategy",
            "name": "Test Created Strategy",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
        }
        with TemporaryDirectory() as tmp:
            loader = StrategyLoader(base_dir=tmp)
            saved = loader.save(strategy)
            loaded = loader.load("test_created_strategy")
            self.assertEqual("test_created_strategy", saved["id"])
            self.assertEqual("Test Created Strategy", loaded["name"])
            self.assertEqual(20, loaded["entry"]["children"][0]["right"]["params"]["period"])
            self.assertEqual("all", loaded["entry"]["combinator"])
            self.assertEqual("any", loaded["exit"]["combinator"])

    def test_loader_saves_new_strategy_to_db_when_available(self) -> None:
        strategy = {
            "schema_version": 1,
            "id": "test_created_db_strategy",
            "name": "Test Created DB Strategy",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
        }
        with TemporaryDirectory() as tmp:
            strategy_dir = Path(tmp) / "strategies"
            loader = StrategyLoader(
                base_dir=strategy_dir,
                db=Database(Path(tmp) / "trend_quant.db"),
            )
            saved = loader.save(strategy)
            loaded = loader.load("test_created_db_strategy")
            listed = {item["id"]: item for item in loader.list_strategies()}

            self.assertEqual("db", saved["storage"])
            self.assertFalse((strategy_dir / "test_created_db_strategy.yaml").exists())
            self.assertEqual("Test Created DB Strategy", loaded["name"])
            self.assertEqual("db", listed["test_created_db_strategy"]["storage"])

    def test_service_generates_strategy_id_when_missing(self) -> None:
        strategy = {
            "schema_version": 1,
            "name": "中文策略名称",
            "trade_mode": "single_symbol_all_in",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": ">=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {
                        "type": "condition",
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {
                            "type": "indicator",
                            "name": "sma",
                            "params": {"field": "close", "period": 20},
                        },
                    }
                ],
            },
        }
        with TemporaryDirectory() as tmp:
            service = RuleBacktestService(
                strategy_loader=StrategyLoader(base_dir=Path(tmp) / "strategies", db=Database(Path(tmp) / "trend_quant.db")),
                market_store=object(),
            )
            saved = service.save_strategy(strategy)

            self.assertTrue(saved["id"].startswith("strategy_"))
            self.assertEqual("db", saved["storage"])
            self.assertEqual("中文策略名称", service.strategy_loader.load(saved["id"])["name"])

    def test_service_runs_with_fake_market_store(self) -> None:
        class FakeMarketStore:
            def load_history(self, symbol: str) -> pd.DataFrame:
                self.symbol = symbol
                return make_bars(([10.0] * 20) + ([12.0] * 5) + ([8.0] * 5))

            def list_stored_symbols(self) -> list[str]:
                return ["TEST"]

        with TemporaryDirectory() as tmp:
            Path(tmp, "close_above_sma20.yaml").write_text(
                yaml.safe_dump(SAMPLE_STRATEGY, allow_unicode=True), encoding="utf-8"
            )
            service = RuleBacktestService(strategy_loader=StrategyLoader(base_dir=tmp), market_store=FakeMarketStore())
            result = service.run(
                {
                    "strategy_id": "close_above_sma20",
                    "symbol": "TEST",
                    "initial_capital": 100000.0,
                    "slippage": 0.0,
                    "fee_rate": 0.0,
                    "fee_min": 0.0,
                }
            )
            self.assertEqual("ok", result["status"])
            self.assertEqual(["BUY", "SELL"], [trade["side"] for trade in result["trades"]])

    def test_service_meta_tolerates_uninitialized_market_store(self) -> None:
        class UninitializedMarketStore:
            def list_stored_symbols(self) -> list[str]:
                raise RuntimeError("Database not initialized")

        service = RuleBacktestService(strategy_loader=StrategyLoader(), market_store=UninitializedMarketStore())
        instruments = service.list_instruments()
        self.assertTrue(isinstance(instruments, list))
        if instruments:
            self.assertFalse(instruments[0]["has_market_data"])

    def test_service_exposes_indicator_metadata(self) -> None:
        service = RuleBacktestService(strategy_loader=StrategyLoader(), market_store=object())
        indicators = service.list_indicators()
        ids = {item["id"] for item in indicators}
        self.assertIn("sma", ids)
        self.assertIn("bias_atr_normed", ids)


class CooldownStateValueTest(unittest.TestCase):
    """days_since_last_exit（离场冷却期）：卖出后至少过 X 个交易日才能再入场。"""

    def _run(self, strategy: dict, bars: pd.DataFrame) -> dict:
        return SingleSymbolAllInBacktestEngine().run(
            RuleBacktestRequest(
                strategy=strategy,
                symbol="TEST",
                bars=bars,
                execution=BacktestExecutionConfig(slippage=0.0, fee_rate=0.0, fee_min=0.0),
            )
        )

    @staticmethod
    def _trade_day_indices(result: dict, bars: pd.DataFrame) -> dict[str, list[int]]:
        day_to_idx = {str(day): idx for idx, day in enumerate(bars["date"])}
        out: dict[str, list[int]] = {"BUY": [], "SELL": []}
        for trade in result["trades"]:
            out[trade["side"]].append(day_to_idx[trade["date"]])
        return out

    @staticmethod
    def _cooldown_strategy(min_days: float) -> dict:
        return {
            "id": "cooldown",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {  # 恒真：没有冷却限制时每天都想入场
                        "left": {"type": "literal", "value": 1},
                        "operator": ">=",
                        "right": {"type": "literal", "value": 0},
                    },
                    {
                        "left": {"type": "state_value", "name": "days_since_last_exit"},
                        "operator": ">=",
                        "right": {"type": "literal", "value": min_days},
                    },
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "left": {"type": "price", "field": "close"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 95},
                    }
                ],
            },
        }

    def test_cooldown_blocks_reentry_within_x_days(self) -> None:
        # idx 4 和 idx 8 收盘 90 → 触发卖出；其余日子恒真条件想入场。
        closes = [100.0] * 4 + [90.0] + [100.0] * 3 + [90.0] + [100.0] * 5
        bars = make_bars(closes)
        result = self._run(self._cooldown_strategy(3), bars)
        indices = self._trade_day_indices(result, bars)

        self.assertEqual([4, 8], indices["SELL"])
        # idx 0：从未卖出 → 冷却条件视为满足，首次入场不被阻塞。
        # 卖出日 idx 4 → 差值 idx5=1、idx6=2 均被挡，idx7=3 才允许再入场；
        # 卖出日 idx 8 → 同理 idx11 再入场。
        self.assertEqual([0, 7, 11], indices["BUY"])

    def test_never_sold_gte_passes_but_lte_does_not(self) -> None:
        bars = make_bars([100.0] * 10)
        # `<= X` 与 None（从未卖出）比较不通过 → 全程无交易。
        strategy = self._cooldown_strategy(3)
        strategy["entry"]["children"][1]["operator"] = "<="
        result = self._run(strategy, bars)
        self.assertEqual([], result["trades"])

    def test_reset_preserves_last_exit_bar_idx(self) -> None:
        # 冷却期状态是账户级历史，持仓 reset 不得清除。
        position = PositionState(qty=100, entry_price=10.0)
        position.last_exit_bar_idx = 4
        position.reset()
        self.assertFalse(position.is_open)
        self.assertEqual(4, position.last_exit_bar_idx)


if __name__ == "__main__":
    unittest.main()
