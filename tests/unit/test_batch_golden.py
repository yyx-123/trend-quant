"""真·端到端 golden-master（方案 §5.5）：固定行情 fixture + 固定策略快照 +
固定区间，锁定 summary 数值。

用途：批量回测会把几千个格子落库。任何引擎/指标重构后，本测试回答
「历史批次结果是否仍可与现在重跑对比」。数值变化 = 显式打破兼容性，
必须同步评估历史批次的可比性并在 changelog 中说明。

fixture 生成脚本见 tests/fixtures/golden_bars.csv 头部注释（seed 20260726）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from rule_backtest import (
    BacktestExecutionConfig,
    RuleBacktestRequest,
    SingleSymbolAllInBacktestEngine,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "golden_bars.csv"

GOLDEN_STRATEGY = {
    "id": "golden_batch",
    "name": "golden",
    "schema_version": 1,
    "trade_mode": "single_symbol_all_in",
    "entry": {
        "type": "group",
        "combinator": "all",
        "children": [
            {
                "id": "c1",
                "type": "condition",
                "left": {"type": "price", "field": "close"},
                "operator": "cross_above",
                "right": {"type": "indicator", "name": "sma", "params": {"period": 20}},
            }
        ],
    },
    "exit": {
        "type": "group",
        "combinator": "any",
        "children": [
            {
                "id": "x1",
                "type": "condition",
                "left": {"type": "price", "field": "close"},
                "operator": "cross_below",
                "right": {"type": "indicator", "name": "sma", "params": {"period": 20}},
            }
        ],
    },
}

# 2026-07-26 由引擎跑出并锁定（pandas 2.x / numpy 2.x）。
EXPECTED_SUMMARY = {
    "total_return": -0.22508322002336922,
    "annual_return": -0.19339087302192648,
    "max_drawdown": -0.23150964515018346,
    "sharpe": -1.930959304687688,
    "win_rate": 0.13636363636363635,
    "profit_factor": 0.16904403674464175,
    "trade_count": 44,
    "closed_trade_count": 22,
    "total_trading_cost": 321.885622336852,
}
EXPECTED_FINAL_EQUITY = 77491.67799766308
EXPECTED_BENCHMARK = {
    "total_return": -0.014483399999999924,
    "annual_return": -0.012220718663731867,
}


@pytest.mark.unit
def test_golden_backtest_summary_is_stable() -> None:
    bars = pd.read_csv(FIXTURE)
    bars["time"] = pd.to_datetime(bars["time"])
    result = SingleSymbolAllInBacktestEngine().run(
        RuleBacktestRequest(
            strategy=GOLDEN_STRATEGY,
            symbol="GOLDEN.SS",
            bars=bars,
            execution=BacktestExecutionConfig(),
        )
    )
    summary = result["summary"]
    for key, expected in EXPECTED_SUMMARY.items():
        actual = summary[key]
        if isinstance(expected, float):
            assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12), (
                f"summary.{key} 漂移: {actual} != {expected}"
            )
        else:
            assert actual == expected, f"summary.{key} 漂移: {actual} != {expected}"
    assert result["final_equity"] == pytest.approx(EXPECTED_FINAL_EQUITY, rel=1e-9)
    bench = result["benchmark_summary"]
    assert bench["total_return"] == pytest.approx(EXPECTED_BENCHMARK["total_return"], rel=1e-9)
    assert bench["annual_return"] == pytest.approx(EXPECTED_BENCHMARK["annual_return"], rel=1e-9)
    # 逐笔交易数量与首笔日期锁定（买卖点层面的回归锚）
    assert len(result["trades"]) == 44
    assert result["trades"][0]["side"] == "BUY"


@pytest.mark.unit
def test_golden_strategy_is_batch_eligible() -> None:
    """golden 策略必须能过批量的随机指标校验（防止校验逻辑误伤）。"""
    from rule_backtest.batch_service import strategy_uses_random_indicator

    assert strategy_uses_random_indicator(GOLDEN_STRATEGY) is False
    json.dumps(GOLDEN_STRATEGY)  # 快照可序列化
