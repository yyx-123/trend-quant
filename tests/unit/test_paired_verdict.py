"""配对判定口径的验收钉子（评审 DS-P1-6）。

统计事实（已写进 verdict_rules 与开发日志）：配对检验 t ≈ ΔSharpe×√(n/252)，
与相关性无关——10 年日频（n=2500）上 ΔSharpe=0.2 → t≈0.63，对任何诚实检验
都不可达 confirmed（DS-R2 验收行"0.2→confirmed"在统计上不成立，诚实检出
下限 ≈0.5）；本文件钉住的是**门槛行为**（t≥1.645 单尾 + DSR(diff)>0 +
ΔSharpe>0 + 非孤峰 + 无塌陷）与配对实现的解析正确性。

配对口径的核心修复：显著性判定作用于**差序列**（r_exp − r_base），而不是
把基准已实现 Sharpe 当已知常数的单序列 PSR。
"""

from __future__ import annotations

import numpy as np
import pytest

from research.stats.paired import paired_sharpe_comparison
from research.verdict_rules import suggest_backtest_verdict

pytestmark = pytest.mark.unit


def _paired_series(delta_annual: float, n: int = 2500, rho: float = 0.98, seed: int = 3):
    rng = np.random.default_rng(seed)
    shared = rng.normal(0.0005, 0.01, n)
    idio = rng.normal(0.0, 0.01 * np.sqrt(1 - rho**2), n)
    sigma_diff = 0.01 * np.sqrt(1 - rho**2)
    delta_daily = delta_annual * sigma_diff / np.sqrt(252)
    return shared + delta_daily + idio, shared


def test_paired_t_stat_matches_analytic():
    """配对 t 统计与解析值一致：构造常数差序列 diff = δ + 微小噪声，
    t ≈ δ/(σ/√n)（独立口径：直接用 numpy 手写，不复用实现）。"""
    n = 2500
    rng = np.random.default_rng(1)
    base = rng.normal(0.0005, 0.01, n)
    diff_noise = rng.normal(0.0, 0.001, n)
    delta = 0.0005  # 日收益差常数
    exp = base + delta + diff_noise
    out = paired_sharpe_comparison(exp, base, n_trials=1)
    assert out is not None
    # 解析期望：t = mean(diff)/(std(diff)·n^{-1/2}) ≈ 0.0005/(0.001/50) = 25
    assert out["t_stat"] == pytest.approx(25.0, rel=0.1)
    assert out["delta_sharpe_annual"] == pytest.approx(0.0005 / 0.001 * np.sqrt(252), rel=0.1)


def test_gate_confirmed_when_strong_real_improvement():
    """ΔSharpe=0.8（10 年，t≈2.5 可达）+ 配对显著 → confirmed。"""
    exp, base = _paired_series(0.8, seed=7)
    paired = paired_sharpe_comparison(exp, base, n_trials=4)
    assert paired["t_stat"] >= 1.645
    assert paired["dsr_on_diff"] > 0
    evidence = {
        "deltas_vs_base": {"delta_sharpe": paired["delta_sharpe_annual"]},
        "stats": {"paired": paired},
        "plateau": {"verdict": "plateau"},
        "regime_split": {},
    }
    assert suggest_backtest_verdict(evidence) == "confirmed"


def test_gate_inconclusive_below_detection_floor():
    """ΔSharpe=0.2（t≈0.63 < 1.645）→ inconclusive（诚实检出下限）。"""
    exp, base = _paired_series(0.2, seed=11)
    paired = paired_sharpe_comparison(exp, base, n_trials=1)
    if paired is not None and paired["t_stat"] < 1.645:
        evidence = {
            "deltas_vs_base": {"delta_sharpe": paired["delta_sharpe_annual"]},
            "stats": {"paired": paired},
            "plateau": None,
            "regime_split": {},
        }
        assert suggest_backtest_verdict(evidence) == "inconclusive"


def test_gate_zero_edge_never_confirmed():
    """ΔSharpe=0（真零边际）：多 seed 下 confirmed 率 ≤ 5%（名义单尾）。"""
    confirmed = 0
    trials = 20
    for seed in range(trials):
        exp, base = _paired_series(0.0, seed=seed + 100)
        paired = paired_sharpe_comparison(exp, base, n_trials=1)
        if paired is None:
            continue
        evidence = {
            "deltas_vs_base": {"delta_sharpe": paired["delta_sharpe_annual"]},
            "stats": {"paired": paired},
            "plateau": None,
            "regime_split": {},
        }
        if suggest_backtest_verdict(evidence) == "confirmed":
            confirmed += 1
    assert confirmed / trials <= 0.05 + 1e-9


def test_gate_deterministic_blocks():
    """门槛行为逐项可失败：t 不足 / DSR 不过 / ΔSharpe≤0 / 孤峰 / 塌陷。"""
    good = {"t_stat": 2.0, "dsr_on_diff": 0.5, "band": {"low": 0.01, "high": 0.2},
            "delta_sharpe_annual": 0.6, "n_pairs": 2500, "psr_on_diff": 0.9,
            "diff_sharpe_daily": 0.04}

    def ev(**over):
        paired = dict(good)
        stats_over = over.pop("paired_over", {})
        paired.update(stats_over)
        return {
            "deltas_vs_base": {"delta_sharpe": over.get("delta", 0.6)},
            "stats": {"paired": paired},
            "plateau": over.get("plateau"),
            "regime_split": over.get("regime", {}),
        }

    assert suggest_backtest_verdict(ev()) == "confirmed"
    assert suggest_backtest_verdict(ev(paired_over={"t_stat": 1.0})) == "inconclusive"
    assert suggest_backtest_verdict(ev(paired_over={"dsr_on_diff": -0.1})) == "inconclusive"
    # R24B-F4：证伪也要过配对负向门——点估计恶化但配对不显著 → inconclusive
    assert suggest_backtest_verdict(ev(delta=-0.5)) == "inconclusive"
    assert suggest_backtest_verdict(
        ev(delta=-0.5, paired_over={"t_stat": -6.0, "dsr_on_diff": 0.9})
    ) == "rejected"
    assert suggest_backtest_verdict(ev(plateau={"verdict": "peak"})) == "inconclusive"
    assert suggest_backtest_verdict(ev(regime={"below": {"delta_sharpe": -0.5}})) == "inconclusive"


def test_paired_reproducible_same_seed():
    exp, base = _paired_series(0.2)
    assert paired_sharpe_comparison(exp, base, n_trials=4) ==         paired_sharpe_comparison(exp, base, n_trials=4)


def test_paired_insufficient_samples_returns_none():
    assert paired_sharpe_comparison([0.001] * 10, [0.0] * 10) is None
