"""Tests for the random_uniform indicator (coin-flip condition)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)
from rule_backtest.models import PositionState
from rule_backtest.validators import StrategyConfigValidator
from rule_backtest.value_resolver import ValueResolver

# 默认费率（滑点 0.002 / 佣金率 0.0000854 / 最低佣金 5）——全系统不提供零成本场景
STANDARD_COST = BacktestExecutionConfig()


def make_bars(days: int) -> pd.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for idx in range(days):
        close = 100.0 + idx
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


def draw(resolver: ValueResolver, params: dict) -> float:
    spec = {"type": "indicator", "name": "random_uniform", "params": params}
    value, _ = resolver.resolve(spec, None, PositionState())
    return value


def coin_strategy(seed: int | None, entry_threshold: float, exit_threshold: float) -> dict:
    entry_params = {} if seed is None else {"seed": seed}
    exit_params = {} if seed is None else {"seed": seed + 1000}
    return {
        "id": "coin_flip",
        "entry": {
            "type": "group",
            "combinator": "all",
            "children": [
                {
                    "left": {"type": "indicator", "name": "random_uniform", "params": entry_params},
                    "operator": "<=",
                    "right": {"type": "literal", "value": entry_threshold},
                }
            ],
        },
        "exit": {
            "type": "group",
            "combinator": "any",
            "children": [
                {
                    "left": {"type": "indicator", "name": "random_uniform", "params": exit_params},
                    "operator": "<=",
                    "right": {"type": "literal", "value": exit_threshold},
                }
            ],
        },
    }


def run_engine(strategy: dict, days: int = 10) -> dict:
    return SingleSymbolAllInBacktestEngine().run(
        RuleBacktestRequest(strategy=strategy, symbol="TEST", bars=make_bars(days), execution=STANDARD_COST)
    )


class TestRandomUniformResolver:
    def test_values_are_in_unit_interval(self) -> None:
        resolver = ValueResolver()
        for _ in range(200):
            value = draw(resolver, {"seed": 7})
            assert 0.0 <= value < 1.0

    def test_same_seed_is_reproducible_across_runs(self) -> None:
        r = ValueResolver()
        seq_a = [draw(r, {"seed": 42}) for _ in range(5)]
        # 每个 resolver 是一次独立回测运行；重新创建模拟重跑
        r = ValueResolver()
        seq_b = [draw(r, {"seed": 42}) for _ in range(5)]
        assert seq_a == seq_b

    def test_different_seeds_give_different_sequences(self) -> None:
        r = ValueResolver()
        seq_a = [draw(r, {"seed": 1}) for _ in range(5)]
        r = ValueResolver()
        seq_b = [draw(r, {"seed": 2}) for _ in range(5)]
        assert seq_a != seq_b

    def test_missing_seed_uses_entropy_and_differs_between_runs(self) -> None:
        r1, r2 = ValueResolver(), ValueResolver()
        seq_a = [draw(r1, {}) for _ in range(5)]
        seq_b = [draw(r2, {}) for _ in range(5)]
        assert seq_a != seq_b

    def test_sequence_is_independent_of_bars(self) -> None:
        # random_uniform 不依赖 K 线；同一天重复求值也会推进序列（由组短路保证每天只评估一次）
        resolver = ValueResolver()
        values = {draw(resolver, {"seed": 9}) for _ in range(50)}
        assert len(values) == 50


class TestRandomUniformValidator:
    @staticmethod
    def _strategy_with_params(params: dict | None) -> dict:
        right: dict = {"type": "indicator", "name": "random_uniform"}
        if params is not None:
            right["params"] = params
        return {
            "id": "coin_flip",
            "entry": {
                "type": "group",
                "combinator": "all",
                "children": [
                    {"left": right, "operator": "<=", "right": {"type": "literal", "value": 0.5}}
                ],
            },
            "exit": {
                "type": "group",
                "combinator": "any",
                "children": [
                    {
                        "left": {"type": "indicator", "name": "random_uniform"},
                        "operator": "<=",
                        "right": {"type": "literal", "value": 0.1},
                    }
                ],
            },
        }

    def test_seed_is_optional_and_defaults_to_none(self) -> None:
        result = StrategyConfigValidator().validate_and_normalize(self._strategy_with_params(None))
        assert result.ok, result.errors
        spec = result.normalized["entry"]["children"][0]["left"]
        assert spec["params"] == {"seed": None}

    def test_seed_is_normalized_to_int(self) -> None:
        result = StrategyConfigValidator().validate_and_normalize(self._strategy_with_params({"seed": 42}))
        assert result.ok, result.errors
        spec = result.normalized["entry"]["children"][0]["left"]
        assert spec["params"]["seed"] == 42

    def test_non_numeric_seed_is_rejected(self) -> None:
        result = StrategyConfigValidator().validate_and_normalize(self._strategy_with_params({"seed": "abc"}))
        assert not result.ok
        assert "seed" in " ".join(result.errors)


class TestRandomUniformBacktest:
    def test_threshold_one_always_triggers(self) -> None:
        # 买入/卖出阈值都是 1.0：每天必买、次日必卖，与随机值无关
        result = run_engine(coin_strategy(seed=42, entry_threshold=1.0, exit_threshold=1.0), days=6)
        sides = [trade["side"] for trade in result["trades"]]
        assert len(sides) >= 4
        assert sides[:4] == ["BUY", "SELL", "BUY", "SELL"]
        assert result["trades"][0]["date"] == "2026-01-01"

    def test_threshold_zero_never_triggers(self) -> None:
        # [0,1) 的随机值不可能 >= 1.0，永远不买入
        strategy = coin_strategy(seed=42, entry_threshold=0.5, exit_threshold=0.5)
        strategy["entry"]["children"][0]["operator"] = ">="
        strategy["entry"]["children"][0]["right"]["value"] = 1.0
        result = run_engine(strategy)
        assert result["trades"] == []

    def test_seeded_run_is_reproducible_end_to_end(self) -> None:
        strategy = coin_strategy(seed=42, entry_threshold=0.5, exit_threshold=0.3)
        first = run_engine(strategy)
        second = run_engine(strategy)
        trades_a = [(t["date"], t["side"]) for t in first["trades"]]
        trades_b = [(t["date"], t["side"]) for t in second["trades"]]
        assert trades_a == trades_b
        assert trades_a  # 50%/30% 下 10 个交易日内应当有成交（seed 固定，结果确定）

    def test_unseeded_runs_are_valid(self) -> None:
        # 不传 seed 时结果不可复现，但回测本身应正常完成
        result = run_engine(coin_strategy(seed=None, entry_threshold=1.0, exit_threshold=1.0), days=6)
        sides = [trade["side"] for trade in result["trades"]]
        assert sides[:4] == ["BUY", "SELL", "BUY", "SELL"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
