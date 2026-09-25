"""PSR / DSR / MinTRL（Bailey & López de Prado 2012/2014）。

- PSR(SR*)：Sharpe 估计超越基准 SR* 的概率（按样本长度、偏度、峰度校正）；
- DSR：Deflated Sharpe——按尝试次数 N、收益偏度/峰度校正的 Sharpe 显著性；
- MinTRL：当前 Sharpe 需要多长样本才可信（verdict 展示件）。

公式（Bailey 2012, The Sharpe Ratio Efficient Frontier；2014, The Deflated
Sharpe Ratio）：
    PSR = Φ( (SR̂ − SR*) · √(n−1) / √(1 − γ3·SR̂ + (γ4−1)/4 · SR̂²) )
    DSR = PSR(SR₀), SR₀ = √V · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))
    MinTRL = 1 + (1 − γ3·SR̂ + (γ4−1)/4·SR̂²) · (Z_α / (SR̂ − SR*))²

Φ 用 math.erf 实现（项目无 scipy 依赖）；测试对拍 statistics.NormalDist
参考实现与论文代数性质（golden 对拍，阶段 5 验收判据）。
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_EULER_MASCHERONI = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    return NormalDist().inv_cdf(p)


def moments(returns) -> tuple[float, float, float, float]:
    """(mean, std, skew, kurtosis-非超额) of returns；样本不足返回 None。"""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 3:
        return 0.0, 0.0, 0.0, 3.0
    mean = float(np.mean(r))
    std = float(np.std(r, ddof=1))
    # R23A-F10：`std <= 0` 挡不住浮点常量序列（[0.01]*10 的 std=1.8e-18 > 0 →
    # 会算出 skew=1.19、kurt=0.43 这种物理上不可能的矩，再喂给 PSR/MinTRL）。
    # 门槛改成与均值同量级的相对判据（与退化腿判据同精神）。
    if std <= max(abs(mean), 1e-12) * 1e-9:
        return mean, 0.0, 0.0, 3.0
    skew = float(pd_skew(r))
    kurt = float(pd_kurt(r)) + 3.0  # 公式用非超额峰度
    return mean, std, skew, kurt


def pd_skew(r: np.ndarray) -> float:
    n = len(r)
    m = r.mean()
    s = r.std(ddof=1)
    if s == 0 or n < 3:
        return 0.0
    return float((n / ((n - 1) * (n - 2))) * np.sum(((r - m) / s) ** 3))


def pd_kurt(r: np.ndarray) -> float:
    """超额峰度（Fisher）。"""
    n = len(r)
    m = r.mean()
    s = r.std(ddof=1)
    if s == 0 or n < 4:
        return 0.0
    term = np.sum(((r - m) / s) ** 4)
    return float(
        (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * term
        - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))
    )


def sharpe_of(returns) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return 0.0
    std = float(np.std(r, ddof=1))
    return float(np.mean(r) / std) if std > 0 else 0.0


def psr(sr_hat: float, sr_star: float, n: int, skew: float, kurtosis: float) -> float:
    """PSR(SR*)：SR̂ 超越 SR* 的概率。n 为收益样本数。"""
    if n < 2:
        return 0.0
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr_hat + (kurtosis - 1.0) / 4.0 * sr_hat * sr_hat))
    z = (sr_hat - sr_star) * math.sqrt(n - 1) / denom
    return _norm_cdf(z)


def dsr(sr_hat: float, n: int, skew: float, kurtosis: float, n_trials: int,
        sr_var: float | None = None) -> float:
    """DSR：按尝试次数校正的 Sharpe 显著性。

    N=1（首次尝试）无多重性问题 → 退化为 PSR(0)。

    `sr_var` 缺省时用 **Sharpe 估计量的方差**近似（更强假设：H0 下各试验同真值）。
    注意这与 Bailey & López de Prado (2014) 的 `V[{SR_n}]`（**跨试验** Sharpe 方差）
    不是同一个量——只有调用方显式传入跨试验方差时才与文献口径一致（R23A-F6）。
    """
    if n_trials < 1:
        # R23A-F8：n_trials=0/负数此前与 N=1 同分支（静默不校正）——方向是
        # **放松门**，属危险默认。当前唯一调用点传 max(attempt_index,1)，这里
        # 显式拒绝错误输入，避免未来新调用点把 0 当"首次尝试"。
        raise ValueError(
            f"n_trials must be >= 1 (got {n_trials!r}); pass 1 for a single trial"
        )
    if n_trials == 1:
        return psr(sr_hat, 0.0, n, skew, kurtosis)
    if sr_var is None:
        sr_var = max(
            1e-12,
            (1.0 - skew * sr_hat + (kurtosis - 1.0) / 4.0 * sr_hat * sr_hat) / max(n - 1, 1),
        )
    n_trials = max(int(n_trials), 2)
    sqrt_v = math.sqrt(sr_var)
    term = (
        (1.0 - _EULER_MASCHERONI) * _norm_ppf(1.0 - 1.0 / n_trials)
        + _EULER_MASCHERONI * _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    )
    sr0 = sqrt_v * term
    return psr(sr_hat, sr0, n, skew, kurtosis)


def mintrl(sr_hat: float, sr_star: float, skew: float, kurtosis: float,
           alpha: float = 0.05) -> float | None:
    """MinTRL：SR̂ 显著超越 SR* 所需的最小样本数（日数）。"""
    diff = sr_hat - sr_star
    if diff <= 0:
        return None  # 估计不超基准：多长的样本都救不了
    z = _norm_ppf(1.0 - alpha)
    factor = max(1e-12, 1.0 - skew * sr_hat + (kurtosis - 1.0) / 4.0 * sr_hat * sr_hat)
    return 1.0 + factor * (z / diff) ** 2
