"""Round 18 修复钉子。

- R18A-F1：旧栈**买入持有基准**仍按 100 股为科创板建仓（影响 42 个生产格子的
  benchmark_*/excess_* 列，至少 1 格 excess 变号）。
- R18B-P2-1：`confirmed` 门里的 DSR 条件在阈值 0 下恒真 → 不得声称有折扣（可审计标注）。
- R18B-P2-2：plateau 判据在邻域点 <3 时停止判定（1 点静默判高原 / 2 点 56% 误判孤峰）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _bars(closes: list[float]) -> pd.DataFrame:
    arr = np.asarray(closes, dtype=float)
    n = len(arr)
    return pd.DataFrame({
        "date": pd.bdate_range("2023-01-02", periods=n),
        "time": pd.bdate_range("2023-01-02", periods=n),
        "open": arr, "high": arr, "low": arr, "close": arr,
        "volume": np.full(n, 1e6), "amount": arr * 1e6,
    })


def test_legacy_benchmark_respects_star_min_order():
    """R18A-F1：基准腿也不得用 100 股为科创板建仓。

    688795.SS 首收约 600 元 → 10 万初始资金只够 100 股（<200 最小申报）→
    基准应为**全程现金**（qty=0），而不是"100 股买入持有"。
    """
    from rule_backtest.engine import SingleSymbolAllInBacktestEngine

    eng = SingleSymbolAllInBacktestEngine
    bars = _bars([600.5] + [610.0] * 9)
    star = eng._buy_and_hold_benchmark(
        bars=bars, initial_capital=100_000.0, lot_size=100,
        symbol="688795.SS", asset_type="stock",
    )
    assert star["qty"] == 0, "科创板基准买不起 200 股时应为全程现金"
    assert all(row["equity"] == 100_000.0 for row in star["series"])
    # 资金足够 200 股 → 正常建仓且 ≥200
    rich = eng._buy_and_hold_benchmark(
        bars=bars, initial_capital=200_000.0, lot_size=100,
        symbol="688795.SS", asset_type="stock",
    )
    assert rich["qty"] >= 200, rich["qty"]
    # 非科创板不受影响
    main = eng._buy_and_hold_benchmark(
        bars=bars, initial_capital=100_000.0, lot_size=100,
        symbol="600519.SS", asset_type="stock",
    )
    assert main["qty"] == 100


def test_dsr_gate_binding_is_auditable():
    """R18B-P2-1：DSR 门在阈值 0 下恒真——必须可审计地标注"无折扣"。"""
    from research.verdict_rules import DEFAULT_RULES, dsr_gate_binding

    assert DEFAULT_RULES["min_dsr_on_diff"] == 0.0
    assert dsr_gate_binding() is False, "阈值 0 → 该门恒真、无约束力"
    assert dsr_gate_binding({"min_dsr_on_diff": 0.95}) is True, "论文口径下应具备约束力"


def test_plateau_insufficient_neighbors_is_not_a_verdict():
    """R18B-P2-2：邻域点 <3 时不得给随机答案（1 点曾静默判高原、2 点 56% 误判孤峰）。"""
    from research.verdict_rules import plateau_verdict

    for neighbors in ([0.5], [0.1, 0.2]):
        out = plateau_verdict(3.0, neighbors)
        assert out["verdict"] == "unknown", out
        assert out["insufficient_neighbors"] is True
        assert "neighbor_points_insufficient" in out["reason"]
    # 3 点起恢复正常判定：邻域同向且未偏离 1σ → 高原
    assert plateau_verdict(3.0, [2.9, 3.05, 2.95])["verdict"] == "plateau"
    # 孤立峰（选定值远超邻域）→ peak
    assert plateau_verdict(9.0, [0.1, 0.12, 0.11])["verdict"] == "peak"
