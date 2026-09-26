"""区块 bootstrap（circular block）与蒙特卡洛置信带（详设 §6.5.1 G 项）。

- sharpe_block_bootstrap：日收益序列的环形区块 bootstrap → Sharpe 置信带；
- trade_bootstrap_bands：round-trip 净盈亏序列重抽样 → 终值/最大回撤
  置信带（MC 置信带，交易序列 bootstrap）。

随机性全部由传入 seed 决定——同种子位级可复现（台账可复审的前提）。
"""

from __future__ import annotations

import numpy as np


def _circular_blocks(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """环形区块重抽样的下标序列（长度 n）。"""
    idx = np.empty(n, dtype=int)
    pos = 0
    while pos < n:
        start = int(rng.integers(0, n))
        length = min(block, n - pos)
        block_idx = (start + np.arange(length)) % n
        idx[pos: pos + length] = block_idx
        pos += length
    return idx


def sharpe_block_bootstrap(
    returns,
    *,
    n_boot: int = 1000,
    block: int | None = None,
    seed: int = 7,
) -> dict:
    """Sharpe 的区块 bootstrap 置信带（年化 Sharpe 口径，252 交易日）。

    返回 {point, low, high, n_boot, block}；样本不足返回 None 字段。
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 30:
        return {"point": None, "low": None, "high": None, "n_boot": 0}
    block = block or max(2, int(round(np.sqrt(n))))
    point = float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 0 else 0.0

    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot)
    for b in range(n_boot):
        sample = r[_circular_blocks(n, block, rng)]
        std = sample.std(ddof=1)
        stats[b] = sample.mean() / std * np.sqrt(252) if std > 0 else 0.0
    return {
        "point": point,
        "low": float(np.percentile(stats, 2.5)),
        "high": float(np.percentile(stats, 97.5)),
        "n_boot": n_boot,
        "block": block,
    }


def _acf1(p: np.ndarray) -> float:
    """lag-1 自相关（无依赖实现）。"""
    if p.size < 3:
        return 0.0
    x = p - p.mean()
    denom = float(np.dot(x, x))
    return float(np.dot(x[:-1], x[1:]) / denom) if denom > 0 else 0.0


def trade_bootstrap_bands(
    pnls,
    *,
    initial_equity: float,
    n_boot: int = 1000,
    seed: int = 7,
    block: int | None = None,
) -> dict:
    """MC 置信带：round-trip 净盈亏序列重抽 → 净值路径 → 终值/回撤分布。

    R24A-F6（P2）：重抽样按**区块**（默认块长按 lag-1 自相关自动定，至少 2）——
    逐笔 PnL 有强序列相关（真实 E0002 腿 lag1..5 = 0.23/0.35/0.19/0.27），
    事件级 iid 重抽样把终值带宽低估 **1.77×**、回撤尾带低估 17%~27%
    （AR(1) ρ=0.3 时名义 95% 的实际覆盖降到 84%）。块长与实测自相关一并落库。

    pnls: 逐笔净盈亏金额序列（round-trip pnl_net）。
    返回 {final_equity: {point, low, high}, max_drawdown: {point, low, high},
          method, block, acf1}。
    """
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    if len(p) < 5:
        return {"final_equity": None, "max_drawdown": None}
    acf1 = _acf1(p)
    if block is None:
        # 块长按经验规则 n^(1/3)（序列相关下的常用折中，n=445 → 8），并保证
        # 覆盖 AR(1) 的积分相关时间（ρ>0 时至少 (1+ρ)/(1−ρ) 取整）。
        rho = max(0.0, min(acf1, 0.95))
        target = max(round(len(p) ** (1.0 / 3.0)), round((1.0 + rho) / max(1e-6, 1.0 - rho)))
        block = int(max(5, min(target, 50)))
        # R25A-F10：块长不得 ≥ 序列长度（否则单块循环重抽 → 终值带宽恒为 0）
        block = int(max(2, min(block, max(1, len(p) // 3))))

    def _path_stats(seq: np.ndarray) -> tuple[float, float]:
        equity = initial_equity + np.cumsum(seq)
        final = float(equity[-1])
        peak = np.maximum.accumulate(equity)
        dd = float((equity / peak - 1.0).min())
        return final, dd

    point_final, point_dd = _path_stats(p)
    rng = np.random.default_rng(seed)
    finals = np.empty(n_boot)
    dds = np.empty(n_boot)
    n = len(p)
    n_blocks_need = int(np.ceil(n / block))
    starts_pool = np.arange(n)
    for b in range(n_boot):
        # 循环区块重抽样（保持序列内的局部依赖结构）
        idx = np.concatenate([
            (s + np.arange(block)) % n for s in rng.choice(starts_pool, n_blocks_need)
        ])[:n]
        sample = p[idx]
        finals[b], dds[b] = _path_stats(sample)
    return {
        "final_equity": {
            "point": point_final,
            "low": float(np.percentile(finals, 2.5)),
            "high": float(np.percentile(finals, 97.5)),
        },
        "max_drawdown": {
            "point": point_dd,
            "low": float(np.percentile(dds, 2.5)),
            "high": float(np.percentile(dds, 97.5)),
        },
        "n_boot": n_boot,
        # R24A-F6/R24A-F7：口径与依赖结构如实落库（读者据此判断带宽是否可信）
        "method": "circular_block",
        "block": int(block),
        "acf1": round(float(acf1), 4),
    }
