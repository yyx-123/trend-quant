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
        # R8 复核（P2）：退化列（全现金/零成交）的 `_sharpe_vec` 是 1e12 级噪声，
        # 会在每个 CSCV 组合里**同时**赢下 IS argmax 与 OOS 最优 → λ≡1 → pbo=0.0
        # （假的"绝不拟合"）。退化列不参与比较：全退化则本组合不产生 λ。
        _usable = np.isfinite(is_sharpe) & np.isfinite(oos_sharpe) & (
            np.abs(is_sharpe) <= _SHARPE_ABS_LIMIT
        )
        if not _usable.any():
            continue
        _is_masked = np.where(_usable, is_sharpe, -np.inf)
        best_is = int(np.argmax(_is_masked))
        # OOS 相对秩（0..1；λ<0.5 = IS 最优在 OOS 中位数以下 = 过拟合）。
        # R7 复核（R7B #1）：**同名次按半步计**——变体互不相上下（乃至所有变体
        # 是同一条 run）时，`oos < best` 会把并列全判为"更差"→ 秩 0 → λ=0 →
        # pbo=1.0（"必然过拟合"），与同一批证据里的 `plateau=plateau` 自相矛盾。
        _best_oos = oos_sharpe[best_is]
        _oos_usable = oos_sharpe[_usable]
        rank = float(np.sum(_oos_usable < _best_oos)) + 0.5 * float(
            np.sum(_oos_usable == _best_oos) - 1
        )
        lam = rank / (len(_oos_usable) - 1) if len(_oos_usable) > 1 else 0.0
        lambdas.append(lam)

    if not lambdas:
        # 所有变体都退化（零成交/全现金）→ PBO 无判别力
        return {"pbo": None, "n_combinations": comb(n_blocks, half),
                "lambda_median": None, "degenerate_variants": True}
    lambdas = np.array(lambdas)
    pbo = float(np.mean(lambdas < 0.5))
    return {
        "pbo": pbo,
        "n_combinations": comb(n_blocks, half),
        "lambda_median": float(np.median(lambdas)),
        # R7B #1：全并列（含"所有变体同一条 run"）时 λ 恒 0.5、PBO 无判别力——
        # 显式标注，避免把 0.0 读成"绝不过拟合"
        "degenerate_variants": bool(np.all(lambdas == 0.5)),
    }


_SHARPE_ABS_LIMIT = 50.0  # 与 rule_backtest.metrics.DEGENERATE_SHARPE_ABS_LIMIT 同口径


def _sharpe_vec(x: np.ndarray) -> np.ndarray:
    """逐列 Sharpe；**退化列记 NaN**（不可参与比较，R8 复核）。

    退化 = 方差不可分辨（std <= |mean|·1e-6）或幅值超过闸门（|sharpe| > 50）——
    零成交/全现金列的 1e12 级噪声会赢下每个 CSCV 组合的 argmax，把 PBO 伪造成 0。
    """
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0, ddof=1)
    with np.errstate(all="ignore"):
        sharpe = np.where(std > 0, mean / std, 0.0)
    degenerate = (std <= np.abs(mean) * 1e-6) | (np.abs(sharpe) > _SHARPE_ABS_LIMIT)
    return np.where(degenerate, np.nan, sharpe)
