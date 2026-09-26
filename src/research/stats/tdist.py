"""t 分布尾概率与分位数（**无第三方依赖**）。

为什么自己写：判定主路径（plateau 的 95% 预测区间、配对检验的 p 值、小样本 t 临界值）
都需要 t 分布，而项目**声明不依赖 scipy**（`pyproject.toml` 无 scipy、部署用
`.venv` 里也没有）。R24B-F1 实测过：在 `.venv` 下任何 import scipy 的判定路径都会
把实验直接打成 failed。这里用正则化不完全贝塔的连分式（Numerical Recipes 的
`betacf`）+ 二分反演实现，精度到 1e-12，与 scipy 对拍见 `test_loop_review_ds4f_r24.py`。
"""

from __future__ import annotations

import math

_TINY = 1e-300
_EPS = 3.0e-16
_MAX_ITER = 300


def _betacf(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔的连分式（Lentz 算法）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_beta + b * math.log1p(-x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b


def t_sf(t_stat: float, df: int) -> float:
    """单尾上尾概率 P(T > t)（df 自由度）。"""
    df = max(int(df), 1)
    # R25A-F11：NaN 不得被当成"最显著"（此前 NaN→0.0，经 p 值路径成为最强证据）
    if not math.isfinite(t_stat):
        return 1.0
    x = df / (df + t_stat * t_stat)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return tail if t_stat >= 0 else 1.0 - tail


def t_ppf(p: float, df: int) -> float:
    """t 分布分位数（p ∈ (0,1)）；二分反演 `t_sf`。"""
    df = max(int(df), 1)
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    if p == 0.5:
        return 0.0
    upper = p > 0.5
    target = (1.0 - p) if upper else p      # 目标上尾概率
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > target:
        hi *= 2.0
        if hi > 1e12:
            break
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_sf(mid, df) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(1.0, hi):
            break
    value = 0.5 * (lo + hi)
    return value if upper else -value


def t_critical(p: float, df: int) -> float:
    """单尾临界值 t_{p, df}（p 为单尾置信水平，如 0.95 → 1.645(z 极限)）。"""
    return t_ppf(p, df)
