"""P1.3 golden tests — memoized (full-series) vs legacy (per-day) resolution
must produce bit-identical backtest results across all indicator types."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from rule_backtest import BacktestExecutionConfig, RuleBacktestRequest, SingleSymbolAllInBacktestEngine
from rule_backtest.value_resolver import ValueResolver


def _make_bars(n: int = 260, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 10 + np.cumsum(rng.normal(0.02, 0.2, n))
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "time": pd.to_datetime(dates),
            "open": closes,
            "high": closes + 0.1,
            "low": closes - 0.1,
            "close": closes,
            "volume": np.abs(rng.normal(1e6, 2e5, n)),
        }
    )


def _condition(indicator: str, params: dict, op: str, right: dict, cid: str = "c1") -> dict:
    return {
        "id": cid,
        "type": "condition",
        "left": {"type": "indicator", "name": indicator, "params": params},
        "operator": op,
        "right": right,
    }


def _price_condition(op: str, right: dict, cid: str = "c2") -> dict:
    return {"id": cid, "type": "condition", "left": {"type": "price", "field": "close"}, "operator": op, "right": right}


INDICATOR_STRATEGIES = {
    "sma": _condition("sma", {"period": 20}, "<=", {"type": "indicator", "name": "sma", "params": {"period": 20}}),
    "ema": _condition("ema", {"period": 20}, ">=", {"type": "indicator", "name": "sma", "params": {"period": 20}}),
    "rsi": _condition("rsi", {"period": 14}, "<=", {"type": "literal", "value": 30}),
    "macd": _condition("macd_line", {}, ">=", {"type": "indicator", "name": "macd_signal", "params": {}}),
    "bollinger": _condition("bollinger_lower", {}, ">=", {"type": "price", "field": "close"}),
    "bias": _condition("bias", {"period": 20}, ">=", {"type": "literal", "value": 0.05}),
    "momentum": _condition("momentum_return", {"period": 20}, ">=", {"type": "literal", "value": 0.03}),
    "volume": _condition("volume_ratio", {"period": 20}, ">=", {"type": "literal", "value": 1.5}),
    "trend": _condition("trend_score", {}, ">=", {"type": "literal", "value": 10}),
    "trend_sma": _condition("trend_score_sma", {"period": 5}, ">=", {"type": "literal", "value": 5}),
    "trend_ema": _condition("trend_score_ema", {"period": 5}, ">=", {"type": "literal", "value": 5}),
    "bias_atr": _condition("bias_atr_normed", {}, ">=", {"type": "literal", "value": 1.0}),
    "macd_cross": _condition("macd_line", {}, "cross_above", {"type": "indicator", "name": "macd_signal", "params": {}}),
    "price_cross_sma": _price_condition("cross_above", {"type": "indicator", "name": "sma", "params": {"period": 20}}),
}


def _make_strategy(entry: dict) -> dict:
    return {
        "id": "golden_test",
        "trade_mode": "single_symbol_all_in",
        "entry": {"type": "group", "combinator": "all", "children": [entry]},
        "exit": {
            "type": "group",
            "combinator": "any",
            "children": [
                _price_condition("<=", {"type": "state_value", "name": "hard_stop", "params": {"atr_period": 20, "atr_mul": 1.5}}, "x1"),
                _price_condition("<=", {"type": "state_value", "name": "chandelier_stop", "params": {"atr_period": 20, "atr_mul": 2.5}}, "x2"),
            ],
        },
    }


def _run(engine: SingleSymbolAllInBacktestEngine, strategy: dict, bars: pd.DataFrame, memoize: bool) -> dict:
    if not memoize:
        # Legacy path: no context bars → every resolution computes per-day.
        original = ValueResolver.set_context_bars
        ValueResolver.set_context_bars = lambda self, bars: None  # type: ignore[assignment]
        try:
            return engine.run(
                RuleBacktestRequest(
                    strategy=strategy,
                    symbol="TEST",
                    bars=bars,
                    execution=BacktestExecutionConfig(initial_capital=100000.0),
                )
            )
        finally:
            ValueResolver.set_context_bars = original  # type: ignore[assignment]
    return engine.run(
        RuleBacktestRequest(
            strategy=strategy,
            symbol="TEST",
            bars=bars,
            execution=BacktestExecutionConfig(initial_capital=100000.0),
        )
    )


@pytest.mark.parametrize("key", sorted(INDICATOR_STRATEGIES))
def test_memoized_matches_legacy(key: str) -> None:
    bars = _make_bars()
    strategy = _make_strategy(INDICATOR_STRATEGIES[key])
    engine = SingleSymbolAllInBacktestEngine()
    legacy = _run(engine, strategy, bars, memoize=False)
    cached = _run(engine, strategy, bars, memoize=True)
    assert cached["trades"] == legacy["trades"], f"{key}: trades diverged"
    assert cached["daily_nav"] == legacy["daily_nav"], f"{key}: nav diverged"


def test_seeded_random_matches_legacy() -> None:
    bars = _make_bars()
    strategy = _make_strategy(
        _condition("random_uniform", {"seed": 42}, ">=", {"type": "literal", "value": 0.5})
    )
    engine = SingleSymbolAllInBacktestEngine()
    legacy = _run(engine, strategy, bars, memoize=False)
    cached = _run(engine, strategy, bars, memoize=True)
    assert cached["trades"] == legacy["trades"]
    assert cached["daily_nav"] == legacy["daily_nav"]
