"""FDR（Benjamini-Hochberg）与 PBO（CSCV，详设 §6.10.2 B/D 项）。

- bh_fdr(pvals, q)：一批实验 p 值的 BH 校正——返回每个的 adjusted 判定；
- pbo_cscv(returns_matrix, S)：CSCV 回测过拟合概率（Bailey et al. 2014
  "Pseudo-mathematics of backtest overfitting"）：T×N 策略收益矩阵分 S 块，
  逐组合 IS 最优策略的 OOS 相对秩 λ，PBO = P(logit(λ) < 0)。
"""

from __future__ import annotations

from itertools import combinations
from math import comb

import numpy as np


def bh_fdr(pvalues, q: float = 0.05) -> list[dict]:
    """Benjamini-Hochberg。返回 [{"p", "rank", "threshold", "significant"}]。

    adjusted p_i = min_{k≥i} (m/k · p_(k))（单调化），significant = p_i ≤ adj 判定。
    """
    p = np.asarray(pvalues, dtype=float)
    m = len(p)
    if m == 0:
        return []
    order = np.argsort(p)
    sorted_p = p[order]
    adjusted = np.empty(m)
    running = 1.0
    for i in range(m - 1, -1, -1):
        running = min(running, min(1.0, sorted_p[i] * m / (i + 1)))  # R1-P3-16：显式封顶 1.0
        adjusted[i] = running
    out = [None] * m
    for rank, idx in enumerate(order, start=1):
        adj = adjusted[rank - 1]
        out[idx] = {
            "p": float(p[idx]),
            "rank": rank,
            "adjusted_p": float(adj),
            "significant": bool(adj <= q),  # BH 拒绝 ⇔ adjusted p ≤ q
        }
    return out


def pbo_cscv(returns_matrix, *, n_blocks: int = 8) -> dict:
    """CSCV PBO。returns_matrix: (T, N)——T 期收益 × N 个策略变体。

    返回 {pbo, n_combinations, lambda_median}。T 或 N 不足返回 None 字段。
    R1-P3-13：删除从未使用的 seed 参数（此前误导"随机性受控"的可复现性
    表述——CSCV 是全组合枚举，无随机性）；n_blocks 必须为偶数（奇数时
    IS/OOS 块数不等，CSCV 对称性被破坏）。
    """
    r = np.asarray(returns_matrix, dtype=float)
    if r.ndim != 2:
        raise ValueError("returns_matrix must be (T, N)")
    if n_blocks % 2 != 0:
        raise ValueError(f"n_blocks must be even for CSCV (got {n_blocks})")
    t, n = r.shape
    if t < n_blocks * 2 or n < 2:
        return {"pbo": None, "n_combinations": 0, "lambda_median": None}

    blocks = np.array_split(np.arange(t), n_blocks)
    half = n_blocks // 2
    lambdas: list[float] = []
    for combo in combinations(range(n_blocks), half):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(n_blocks) if i not in combo])
        is_sharpe = _sharpe_vec(r[is_idx, :])
        oos_sharpe = _sharpe_vec(r[oos_idx, :])
        best_is = int(np.argmax(is_sharpe))
        # OOS 相对秩（0..1；λ<0.5 = IS 最优在 OOS 中位数以下 = 过拟合）
        rank = int(np.sum(oos_sharpe < oos_sharpe[best_is]))
        lam = rank / (n - 1) if n > 1 else 0.0
        lambdas.append(lam)

    lambdas = np.array(lambdas)
    pbo = float(np.mean(lambdas < 0.5))
    return {
        "pbo": pbo,
        "n_combinations": comb(n_blocks, half),
        "lambda_median": float(np.median(lambdas)),
    }


def _sharpe_vec(x: np.ndarray) -> np.ndarray:
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0, ddof=1)
    with np.errstate(all="ignore"):
        return np.where(std > 0, mean / std, 0.0)
