"""配对显著性检验（评审 DS-P1-6：单序列 PSR 不能冒充配对比较）。

"同一窗口、同一池、只差一个插槽"的改进型实验是天然的高相关配对
（ρ≈0.95–0.99）：未配对 PSR 把基准的已实现 Sharpe 当已知常数、用实验自身
方差作分母——真实 0.2–0.3 年化 ΔSharpe 改进几乎拿不到 confirmed，而低相关
场景又过度宽松（ρ=0 时假阳率 12%）。

配对口径：对**日收益差序列**（r_exp − r_base，同窗同池）做检验——差序列的
Sharpe 就是 ΔSharpe 的日频形态；区块 bootstrap 给出差序列 Sharpe 的置信带；
DSR 在差序列上按尝试次数校正。PSR/DSR（单序列）保留为展示件。
"""

from __future__ import annotations

import numpy as np

from research.stats.bootstrap import _circular_blocks
from research.stats.psr import dsr, moments, psr


def paired_sharpe_comparison(
    rets_exp,
    rets_base,
    *,
    n_trials: int = 1,
    seed: int = 7,
    n_boot: int = 1000,
) -> dict | None:
    """配对 ΔSharpe 检验。返回 None 当样本不足（<30 个配对日）。

    返回：
    - delta_sharpe_annual：年化 ΔSharpe（差序列 Sharpe × √252 与日频口径一致）；
    - diff_sharpe_daily：差序列日频 Sharpe；
    - t_stat：配对 t 统计；
    - band：差序列 Sharpe 的区块 bootstrap 95% 置信带（含 0 = 不显著）；
    - dsr_on_diff：差序列上的 DSR（尝试次数校正）；
    - n_pairs：配对天数。
    """
    ra = np.asarray(rets_exp, dtype=float)
    rb = np.asarray(rets_base, dtype=float)
    # 长度不等时按尾部位置对齐是静默错配（日期错开时差序列
    # 全错）。调用方必须先按日期交集 join（backtest.py 的既有做法）——
    # 在此显式拒绝错位输入，而不是吞掉。
    if len(ra) != len(rb):
        raise ValueError(
            f"paired comparison requires date-aligned, equal-length series "
            f"(got {len(ra)} vs {len(rb)}); join by date before calling"
        )
    n = len(ra)
    if n < 30:
        return None
    diff = ra - rb
    valid = np.isfinite(diff)
    diff = diff[valid]
    n = len(diff)
    if n < 30:
        return None

    mean_d = float(np.mean(diff))
    std_d = float(np.std(diff, ddof=1))
    diff_sharpe = mean_d / std_d if std_d > 0 else 0.0
    t_stat = mean_d / (std_d / np.sqrt(n)) if std_d > 0 else 0.0

    rng = np.random.default_rng(seed)
    block = max(2, int(round(np.sqrt(n))))
    boot = np.empty(n_boot)
    for b in range(n_boot):
        sample = diff[_circular_blocks(n, block, rng)]
        s_std = sample.std(ddof=1)
        boot[b] = sample.mean() / s_std if s_std > 0 else 0.0
    band = {
        "low": float(np.percentile(boot, 2.5)),
        "high": float(np.percentile(boot, 97.5)),
    }

    _mean, _std, skew, kurt = moments(diff)
    return {
        "delta_sharpe_annual": float(diff_sharpe * np.sqrt(252)),
        "diff_sharpe_daily": float(diff_sharpe),
        "t_stat": float(t_stat),
        "band": band,
        "psr_on_diff": psr(diff_sharpe, 0.0, n, skew, kurt),
        "dsr_on_diff": dsr(diff_sharpe, n, skew, kurt, max(int(n_trials), 1)),
        "n_pairs": int(n),
    }
