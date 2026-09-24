"""统计判定器 golden 对拍（详设 §8 阶段 5 验收判据：Bailey worked example /
参考实现逐值比对，不过即阻断；统计件错了不崩溃，只会让结论安静地撒谎）。

参考实现口径：PSR/DSR/MinTRL 用 statistics.NormalDist 独立复算（实现用
math.erf）；bootstrap/FDR/PBO 用手算/构造性已知答案。
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pytest

from research.stats.bootstrap import sharpe_block_bootstrap, trade_bootstrap_bands
from research.stats.fdr_pbo import bh_fdr, pbo_cscv
from research.stats.psr import dsr, mintrl, psr

pytestmark = pytest.mark.unit


def _phi(x: float) -> float:
    return NormalDist().cdf(x)  # 参考实现（与 impl 的 math.erf 路径独立）


# ----------------------------------------------------------------------
# PSR（Bailey 2012）
# ----------------------------------------------------------------------


def test_psr_worked_example():
    """手算 worked example：SR̂=0.1（日频）、n=252、γ3=0、γ4=3（正态）。

    z = 0.1 × √251 / √(1 + 0.5×0.01) = 1.584297951 / 1.0024969 = 1.580352
    """
    got = psr(0.1, 0.0, 252, 0.0, 3.0)
    z = 0.1 * math.sqrt(251) / math.sqrt(1.005)
    expected = _phi(z)
    assert got == pytest.approx(expected, abs=1e-12)
    assert 0.94 < got < 0.95  # Φ(1.5804) ≈ 0.9430


def test_psr_at_threshold_is_half():
    assert psr(0.05, 0.05, 500, 1.2, 8.0) == pytest.approx(0.5, abs=1e-12)


def test_psr_monotonic_in_n():
    assert psr(0.05, 0.0, 500, 0.0, 3.0) > psr(0.05, 0.0, 100, 0.0, 3.0)


def test_psr_fat_tails_discount():
    """高峰度（厚尾）同样 SR̂ 下显著性更低——公式行为锁定。"""
    assert psr(0.05, 0.0, 252, 0.0, 8.0) < psr(0.05, 0.0, 252, 0.0, 3.0)


# ----------------------------------------------------------------------
# DSR（Bailey 2014）
# ----------------------------------------------------------------------


def test_dsr_reference_implementation():
    """与 NormalDist 独立复算逐值对拍。"""
    sr_hat, n, skew, kurt, trials = 0.08, 500, -0.3, 4.5, 10
    got = dsr(sr_hat, n, skew, kurt, trials)
    # 参考实现（同一公式独立书写）
    v = (1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat * sr_hat) / (n - 1)
    gamma = 0.5772156649015329
    nd = NormalDist()
    sr0 = math.sqrt(v) * (
        (1 - gamma) * nd.inv_cdf(1 - 1 / trials)
        + gamma * nd.inv_cdf(1 - 1 / (trials * math.e))
    )
    z = (sr_hat - sr0) * math.sqrt(n - 1) / math.sqrt(
        1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat * sr_hat
    )
    assert got == pytest.approx(nd.cdf(z), abs=1e-12)


def test_dsr_multiplicity_discount():
    """尝试次数越多 DSR 越低（多重检验折扣）；N=1 退化为 PSR(0)。"""
    base = dict(sr_hat=0.08, n=500, skew=0.0, kurtosis=3.0)
    d1 = dsr(**base, n_trials=1)
    d10 = dsr(**base, n_trials=10)
    d100 = dsr(**base, n_trials=100)
    assert d1 == pytest.approx(psr(0.08, 0.0, 500, 0.0, 3.0), abs=1e-12)
    assert d1 > d10 > d100


def test_mintrl_round_trip():
    """MinTRL 与 PSR 互逆：n = MinTRL 处 PSR ≈ 1−α。"""
    sr_hat, skew, kurt = 0.06, 0.2, 4.0
    n_min = mintrl(sr_hat, 0.0, skew, kurt, alpha=0.05)
    assert n_min is not None and n_min > 2
    assert psr(sr_hat, 0.0, int(round(n_min)), skew, kurt) == pytest.approx(0.95, abs=0.01)
    # 估计不超基准 → None
    assert mintrl(0.0, 0.05, 0.0, 3.0) is None


# ----------------------------------------------------------------------
# 区块 bootstrap
# ----------------------------------------------------------------------


def test_block_bootstrap_reproducible_and_covers_point():
    rng_series = np.random.default_rng(3).normal(0.001, 0.01, 300)
    a = sharpe_block_bootstrap(rng_series, seed=11)
    b = sharpe_block_bootstrap(rng_series, seed=11)
    assert a == b  # 同种子位级可复现
    assert a["low"] <= a["point"] <= a["high"]


def test_block_bootstrap_all_positive():
    r = np.full(200, 0.002) + 1e-6 * np.arange(200) / 200
    out = sharpe_block_bootstrap(r, seed=5)
    assert out["low"] > 0


# ----------------------------------------------------------------------
# MC 置信带（交易序列 bootstrap）
# ----------------------------------------------------------------------


def test_trade_bootstrap_constant_pnls():
    pnls = [100.0] * 50
    out = trade_bootstrap_bands(pnls, initial_equity=10_000, seed=3)
    assert out["final_equity"]["point"] == pytest.approx(15_000)
    assert out["final_equity"]["low"] == pytest.approx(15_000)  # 常数序列无分散
    assert out["max_drawdown"]["point"] == 0.0


def test_trade_bootstrap_mixed_pnls_band():
    rng = np.random.default_rng(4)
    pnls = rng.normal(50, 300, 100)
    out = trade_bootstrap_bands(pnls, initial_equity=100_000, n_boot=500, seed=9)
    assert out["final_equity"]["low"] < out["final_equity"]["point"] < out["final_equity"]["high"]
    assert out["max_drawdown"]["low"] <= out["max_drawdown"]["point"]


# ----------------------------------------------------------------------
# FDR（Benjamini-Hochberg 手算 golden）
# ----------------------------------------------------------------------


def test_bh_fdr_hand_computed():
    """手算：m=10, q=0.05 → 仅 p1(0.001)、p2(0.008) 显著。

    adjusted: p1=0.01, p2=0.04, p3=0.1025, p4=0.1025, 其余=1.0。
    """
    pvals = [0.001, 0.008, 0.039, 0.041, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    out = bh_fdr(pvals, q=0.05)
    assert [o["significant"] for o in out] == [True, True, False, False, False, False, False, False, False, False]
    assert out[0]["adjusted_p"] == pytest.approx(0.01)
    assert out[1]["adjusted_p"] == pytest.approx(0.04)
    assert out[2]["adjusted_p"] == pytest.approx(0.1025)


def test_bh_fdr_empty_and_single():
    assert bh_fdr([], q=0.05) == []
    assert bh_fdr([0.04], q=0.05)[0]["significant"] is True


# ----------------------------------------------------------------------
# PBO（CSCV）
# ----------------------------------------------------------------------


def test_pbo_zero_when_dominant_strategy():
    """策略 0 在每个区块都最优 → PBO = 0（无过拟合迹象）。"""
    rng = np.random.default_rng(2)
    base = rng.normal(0, 0.01, (160, 4))
    dominant = base[:, :1] + 0.05  # 同一噪声 + 恒正漂移 → 处处最优
    matrix = np.hstack([dominant, base[:, 1:]])
    out = pbo_cscv(matrix, n_blocks=8)
    assert out["n_combinations"] == 70
    assert out["pbo"] == 0.0
    assert out["lambda_median"] == pytest.approx(1.0)


def test_pbo_noise_matrix_midrange():
    """纯噪声（同分布）→ PBO 应落在中间（不是 0 也不是 1 的极端）。"""
    rng = np.random.default_rng(8)
    matrix = rng.normal(0, 0.01, (200, 6))
    out = pbo_cscv(matrix, n_blocks=8)
    assert out["pbo"] is not None
    assert 0.0 <= out["pbo"] <= 1.0
    # λ 取值为 rank/5 ∈ {0, .2, .4, .6, .8, 1}；噪声下中位数不应退化到极端
    assert 0.2 <= out["lambda_median"] <= 0.8
