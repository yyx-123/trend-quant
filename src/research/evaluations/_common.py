"""评估模块共享件（详设 §6.5）：面板加载、前瞻收益、无条件对照、平台警告。

所有评估模块的数据只经 L1.5 gateway（as-of 强制）；event/bucket/distribution
在 research 包内向量化自算（"只要数据不要引擎"——L1.5 独立成层的用例）。
"""

from __future__ import annotations

from audit.app_logger import get_logger

_logger = get_logger(__name__)

from datetime import datetime, time
from typing import Any

import numpy as np
import pandas as pd

from gateway.service import Gateway

# 默认流动性池口径（universe=liquidity_default）：近 20 日成交额均值 ≥ 1e8。
DEFAULT_MIN_AMOUNT20 = 1e8


def resolve_universe_symbols(db, spec_universe: Any, panel_probe=None) -> list[str]:
    """spec.universe → 标的清单。

    - "single(510300.SS)"：单标的；
    - "liquidity_default" / 缺省 / None：当前 enabled 全池（流动性过滤在
      面板层做——见 load_eval_panel 的 amount 过滤）；
    - ["AAA", "BBB"]：显式清单。
    """
    if spec_universe is None or spec_universe == "liquidity_default":
        # 元数据经 L1.5 门面（评审 A-P1-2：评估模块不直连 L1）
        from gateway.metadata import MetadataService

        return MetadataService(db).enabled_symbols()
    if isinstance(spec_universe, str) and spec_universe.startswith("single("):
        return [spec_universe[7:-1].strip().upper()]
    if isinstance(spec_universe, (list, tuple)):
        return [str(s).strip().upper() for s in spec_universe]
    raise ValueError(f"unknown universe spec: {spec_universe!r}")


def load_eval_panel(
    db,
    *,
    symbols: list[str],
    start,
    end,
    experiment_id: str,
    min_amount20: float | None = None,
    warnings_out: list | None = None,
):
    """经 L1.5 取评估面板（caller_layer=research，血缘挂在实验 id 上）。

    min_amount20 非空时按"窗口前 20 个交易日均值"过滤流动性（评估口径的
    liquidity_default；时点动态池是数据线二期的事）。
    """
    gateway = Gateway(db)
    as_of = datetime.combine(pd.Timestamp(end).date(), time(15, 0))
    # 垫片 320 自然日（R3A-F4：A 股节假日密集段 300 自然日仅 194~205 交易日，
    # 8/12 抽样窗口起点不足 SMA200 预热——320 保证全部抽样起点 ≥ 200 交易日；
    # DS-复审-R2 §4-2 的预热诉求不变）
    pad_start = (pd.Timestamp(start) - pd.Timedelta(days=320)).date()
    panel = gateway.get_panel(
        symbols=symbols, start=pad_start, end=end,
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="qfq", as_of=as_of, mode="historical",
        caller_layer="research", run_id=experiment_id,
    )
    liquidity_warn = None
    if min_amount20 is not None and panel.dates:
        # 流动性过滤：窗口起点前的 20 日成交额均值（评估口径的可交易池）
        start_day = pd.Timestamp(start).date()
        pre_idx = [i for i, d in enumerate(panel.dates) if d < start_day][-20:]
        # F2（R3A）：垫片期数据不足（窗口起点即数据集起点等）时过滤会
        # 静默失效——此前 `if pre_idx:` 直接跳过，低流动性标的原样入池。
        # 现显式告警（不 fail-loud：评估仍可跑，但口径缩水必须可见）。
        if len(pre_idx) < 20:
            liquidity_warn = (
                f"liquidity filter not fully applied: only {len(pre_idx)}/20 "
                f"pre-window trading days available before {start} "
                f"(pad/数据集起点限制)——低流动性标的可能未被剔除"
            )
        if pre_idx:
            mean_amount = np.nanmean(panel.data["amount"][pre_idx, :], axis=0)
            keep = [
                s for j, s in enumerate(panel.symbols)
                if np.isfinite(mean_amount[j]) and mean_amount[j] >= min_amount20
            ]
            if keep:
                panel = _subset_panel(panel, keep)
    if liquidity_warn:
        _logger.warning("load_eval_panel: %s", liquidity_warn)
        if warnings_out is not None:
            warnings_out.append(liquidity_warn)
    gateway.flush_audit()
    return panel


def _subset_panel(panel, keep: list[str]):
    from gateway.panel import Panel

    idx = [panel._symbol_index[s] for s in keep]
    return Panel(
        dates=panel.dates,
        symbols=tuple(keep),
        data={f: m[:, idx] for f, m in panel.data.items()},
        provisional=panel.provisional[:, idx],
    )


def forward_returns(panel, horizons: list[int], start, end) -> dict[int, np.ndarray]:
    """逐 horizon 的前瞻收益矩阵 (T,N)：close[t+h]/close[t] − 1（qfq）。

    不足 h 日的尾部为 NaN。返回 {h: matrix}。
    """
    close = panel.data["close"]
    out: dict[int, np.ndarray] = {}
    for h in horizons:
        shifted = np.vstack([close[h:], np.full((h, close.shape[1]), np.nan)])
        with np.errstate(all="ignore"):
            out[h] = shifted / close - 1.0
    return out


def unconditional_baseline(
    fwd: dict[int, np.ndarray], panel, start, end
) -> dict[int, dict]:
    """无条件收益分布（全标的 × 全日，同窗口径）——event_study 的对照。"""
    start_day = pd.Timestamp(start).date()
    end_day = pd.Timestamp(end).date()
    in_window = np.array([start_day <= d <= end_day for d in panel.dates])
    out: dict[int, dict] = {}
    for h, matrix in fwd.items():
        vals = matrix[in_window, :]
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            out[h] = {"n": 0}
            continue
        out[h] = {
            "n": len(vals),
            "mean": float(np.mean(vals)),
            "median": float(np.median(vals)),
            "win_rate": float(np.mean(vals > 0)),
            "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0,
        }
    return out


def bootstrap_band(values: np.ndarray, *, n_boot: int = 1000, seed: int = 7) -> dict:
    """事件收益均值的 bootstrap 噪声带（事件级重抽样）。"""
    values = values[np.isfinite(values)]
    if len(values) < 5:
        return {"low": np.nan, "high": np.nan}
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    n = len(values)
    for b in range(n_boot):
        means[b] = values[rng.integers(0, n, n)].mean()
    return {
        "low": float(np.percentile(means, 2.5)),
        "high": float(np.percentile(means, 97.5)),
    }


def regime_labels(panel, benchmark_symbol: str = "510500.SS", ma: int = 200) -> np.ndarray:
    """逐日 regime：benchmark 收盘在 SMA(ma) 上/下（"unknown" 数据不足）。"""
    col = panel._symbol_index.get(benchmark_symbol)
    if col is None:
        return np.array(["unknown"] * len(panel.dates), dtype=object)
    close = panel.data["close"][:, col]
    sma = pd.Series(close).rolling(ma, min_periods=ma).mean().to_numpy()
    labels = np.array(["unknown"] * len(panel.dates), dtype=object)
    valid = np.isfinite(close) & np.isfinite(sma)
    labels[valid] = np.where(close[valid] > sma[valid], "above", "below")
    return labels


def long_window_annotations(start) -> list[str]:
    """长窗口三注记（详设 §6.6.4）：窗口起点 < 2020 时按**窗口**生效——
    所有评估模块共用（DS-复审-R2 §4-1：原只在 backtest 路径，event/bucket/
    distribution 的默认窗口同样是 2015 起却只带通用 survivorship_bias）。
    """
    if str(start) < "2020-01-01":
        return [
            "coverage_note(2015–2019 段仅长历史子集参与，约 1/7 池)",
            "fee_era_mismatch(早期段真实费率更高，成本被低估，结论按保守方向解读)",
            "survivorship_weighting(早期段只含活到今天的标的，偏差按段递增)",
        ]
    return []


def collect_warnings(
    *,
    event_count: int,
    overlap_ratio: float | None = None,
    top_day_share: float | None = None,
    regimes: set[str] | None = None,
    point_in_time_universe: bool = False,
    min_events: int = 30,
) -> list[str]:
    """平台自动警告（详设 §6.6.3；继承被删研究的"共同局限"）。

    point_in_time_universe：数据源是时点动态池才为 True。当前系统还没有
    时点池（数据线二期），一切历史评估都是"当前池穿越历史"——恒带幸存者
    偏差警告。
    """
    warnings: list[str] = []
    if event_count < min_events:
        warnings.append(f"sample_size_small({event_count})")
    if overlap_ratio is not None and overlap_ratio > 0.3:
        warnings.append(f"overlap_heavy({overlap_ratio:.0%} 事件前瞻窗口重叠)")
    if top_day_share is not None and top_day_share > 0.5:
        warnings.append(f"cross_sectional_clustered(top1%日集中 {top_day_share:.0%})")
    if regimes is not None and len(regimes) == 1 and "unknown" not in regimes:
        warnings.append(f"single_regime({next(iter(regimes))})")
    if not point_in_time_universe:
        warnings.append("survivorship_bias(universe 为当前池穿越历史)")
    return warnings


def _bootstrap_means(values, *, n_boot: int = 1000, seed: int = 7):
    """事件均值的 bootstrap 分布（p 值换算用；与 bootstrap_band 同种子同口径）。"""
    values = values[np.isfinite(values)]
    if len(values) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    n = len(values)
    for b in range(n_boot):
        means[b] = values[rng.integers(0, n, n)].mean()
    return means
