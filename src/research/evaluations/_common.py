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
# regime 标签的基准（§6.6.3）：**受流动性过滤豁免**，见 load_eval_panel 的注释
DEFAULT_REGIME_BENCHMARK = "510500.SS"


class EmptyAccount:
    """事件/分桶扫描不需要账户：signal.scan 的 ctx.account 最小只读桩。

    信号插槽协议允许模块读 ``ctx.account``（内置件 abs_momentum@1 会读
    ``positions`` 做"持仓跌出 top 集就退出"）。评估侧扫描上下文必须给足这个
    形状，否则该类模块在 bucket_analysis 下直接 AttributeError 崩掉整个实验
    ——而按平台的计数口径，工程失败**计入 attempt_index**，每次崩溃都在虚增
    DSR 的试验次数 N。
    """

    @property
    def positions(self) -> dict:
        return {}

    def unstopped_symbols(self) -> list[str]:
        return []

    def equity(self) -> float:
        return 0.0

    def heat(self):
        return None


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
    # 垫片 320 自然日（A 股节假日密集段 300 自然日仅 194~205 交易日，
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
            # R24A-F4：基准必须**豁免**流动性过滤。实测基准 510500.SS 的窗口前
            # 20 日均成交额 0.93 亿 < 阈值 1 亿 → 被剔除 → `regime_labels` 全
            # `unknown` → §6.6.3 的 regime 机制（event/bucket 的 regime_split 证据、
            # backtest 的 regime 塌陷否决、single_regime 注记）在真实路径上**整体
            # 失效**，而告警还把它误归因成"2431 日 SMA200 预热不足"。
            keep = [
                s for j, s in enumerate(panel.symbols)
                if (np.isfinite(mean_amount[j]) and mean_amount[j] >= min_amount20)
                or s == DEFAULT_REGIME_BENCHMARK
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


def overlap_and_cluster_stats(events, *, max_h: int, event_days) -> tuple[float | None, float | None]:
    """(前瞻窗口重叠率, top1% 交易日事件集中度)——§6.6.3 两条注记的输入。

    event_study 与 bucket_analysis 共用同一实现。loop-review-ds4f
    此前只有 event 侧算这两个量，bucket 调用
    ``collect_warnings(event_count=...)`` 时既不传 overlap_ratio 也不传
    top_day_share/regimes，导致 §6.6.3 要求"对全部评估模块生效"的五类注记
    里，bucket **结构性**拿不到三类。

    events：可迭代的 (t, col) 或 (t, col, ...) 元组序列（t = 面板行号）。
    event_days：与 events 一一对应的事件日期（用于 top1% 日集中度）。
    """
    events = list(events)
    if not events:
        return None, None
    per_symbol_days: dict[int, list[int]] = {}
    for item in events:
        t, c = int(item[0]), int(item[1])
        per_symbol_days.setdefault(c, []).append(t)
    from itertools import pairwise

    overlaps = sum(
        sum(1 for a, b in pairwise(sorted(ds)) if b - a < max_h)
        for ds in per_symbol_days.values()
    )
    overlap_ratio = overlaps / len(events)
    day_counts = pd.Series(list(event_days)).value_counts()
    top_share = float(day_counts.head(max(1, len(day_counts) // 100)).sum() / len(events))
    return overlap_ratio, top_share


def is_degenerate_leg(nav_rows, *, sharpe: float | None = None) -> bool:
    """该腿是否"退化"（零成交/全现金：日收益只有计息浮点残差，指标不可用）。

    **单一实现**：委托 `rule_backtest.metrics.is_degenerate_nav`（R8 复核后收口；
    此前在 backtest / head_to_head / regime 段各抄一份，该类缺陷因此复发 5 次）。
    """
    from rule_backtest.metrics import is_degenerate_nav

    return is_degenerate_nav(nav_rows, sharpe=sharpe)


def null_degenerate_metrics(summary: dict) -> dict:
    """把退化腿摘要里的噪声指标统一记 None（原地改并返回同一 dict）。"""
    for key in ("sharpe", "sortino"):
        summary[key] = None
    summary["degenerate_leg"] = True
    return summary


def regime_labels(panel, benchmark_symbol: str = DEFAULT_REGIME_BENCHMARK,
                  ma: int = 200, *, warnings_out: list | None = None) -> np.ndarray:
    """逐日 regime：benchmark 收盘在 SMA(ma) 上/下（"unknown" 数据不足）。

    R24A-F4：基准不在面板时**显式告警**（此前静默返回全 unknown，消费方只能
    看到"warmup 不足"这类误导文案）；且统计 unknown 天数与其成因。
    """
    col = panel._symbol_index.get(benchmark_symbol)
    if col is None:
        if warnings_out is not None:
            warnings_out.append(
                f"regime_unavailable(benchmark {benchmark_symbol} not in eval panel"
                "——regime 分段/塌陷否决/single_regime 注记全部缺席)"
            )
        return np.array(["unknown"] * len(panel.dates), dtype=object)
    close = panel.data["close"][:, col]
    sma = pd.Series(close).rolling(ma, min_periods=ma).mean().to_numpy()
    labels = np.array(["unknown"] * len(panel.dates), dtype=object)
    valid = np.isfinite(close) & np.isfinite(sma)
    labels[valid] = np.where(close[valid] > sma[valid], "above", "below")
    if warnings_out is not None and (~valid).any():
        n_unknown = int((~valid).sum())
        total = len(labels)
        # 只有**尾段**未知才真是预热不足；中间未知是基准自身行情缺口
        n_nan_close = int((~np.isfinite(close)).sum())
        reason = (
            f"benchmark price gaps({n_nan_close} 日无收盘)"
            if n_nan_close else f"warmup({ma} 日 SMA 预热)"
        )
        warnings_out.append(
            f"regime_unknown_days({n_unknown}/{total}，{reason})"
        )
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
    """事件均值的 bootstrap 分布（**iid** 事件级重抽样，p 值换算用）。

    R24A-F1：事件按日成簇且前瞻窗口大量重叠时，这个 iid 分布低估均值的抽样
    方差（实测 SE 低估 1.77×；零效应下名义 5% → 实际 17%）。有事件日标签时
    请改用 `cluster_bootstrap_means`（两阶段簇重抽样）。
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    n = len(values)
    for b in range(n_boot):
        means[b] = values[rng.integers(0, n, n)].mean()
    return means


def cluster_bootstrap_means(
    values, event_days, *, n_boot: int = 1000, seed: int = 7
) -> np.ndarray | None:
    """**两阶段簇 bootstrap**：先重抽事件日、再抽该日的事件（R24A-F1）。

    为什么必须按簇：ETF 事件在同一天高度相关（同日均值 ACF(lag1)=0.42）、
    80% 前瞻窗口重叠，事件级 iid 重抽样把 7393 个事件当独立样本 →
    零效应下名义 5% 的检验实际拒绝率 **17%**（我 4000 次 MC 实测；解析设计效应
    `1+(k̄−1)ρ̄≈3.18 → SE 低估 1.78×` 与之吻合）。
    簇口径把"同日多标的"当作一个抽样单位，恢复检验的名义水平。
    """
    values = np.asarray(values, dtype=float)
    days = np.asarray(event_days)
    if len(values) != len(days):
        return None
    keep = np.isfinite(values)
    values, days = values[keep], days[keep]
    if values.size < 5:
        return None
    uniq = np.unique(days)
    if uniq.size < 2:
        return None
    idx_by_day = [np.flatnonzero(days == d) for d in uniq]
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(idx_by_day), len(idx_by_day))
        vals = np.concatenate([values[idx_by_day[i]] for i in pick])
        means[b] = vals.mean()
    return means
