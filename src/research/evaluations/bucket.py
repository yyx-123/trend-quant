"""`bucket_analysis@1` 评估模块（详设 §6.5.3）。

回答的问题形态：这个截面特征有没有排序力。
取证方式：信号日截面按特征分 M 组，看各组前瞻收益单调性。
对照：随机打乱（同一批信号日随机分组的重算结果）。
"""

from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any

import numpy as np
import pandas as pd

from core.indicators import efficiency_ratio
from portfolio.slots.universe import UniverseMember
from research.evaluations._common import (
    DEFAULT_MIN_AMOUNT20,
    EmptyAccount,
    collect_warnings,
    forward_returns,
    load_eval_panel,
    long_window_annotations,
    overlap_and_cluster_stats,
    regime_labels,
    resolve_universe_symbols,
)
from research.evaluations.base import EvaluationModule, register_evaluation
from research.evaluations.event import parse_event_spec

FEATURES = ("atr_pct", "momentum_20", "er_10")


class BucketScanCtx:
    """分桶扫描的逐日上下文（模块级：钉子必须能驱动**真实**上下文与 scan 路径，
    而不是只断言桩对象自身的属性——均实证"只断言桩"的钉子是空钉）。

    事件扫描不需要账户，但信号插槽协议允许模块读 ``ctx.account``（内置件
    abs_momentum@1 会读 ``positions`` 做"持仓跌出 top 集就退出"）。必须给只读
    空账户桩，不能留 None——留 None 时
    bucket_analysis 会 AttributeError 崩掉整个实验（status=failed），而按
    experiments.py 的计数口径工程失败**计入 attempt_index**，每次崩溃都在虚增
    DSR 的试验次数 N。与 event.py 共用 `_common.EmptyAccount`（单一实现）。
    """

    __slots__ = ("date", "panel", "account", "params", "run_seed", "gateway", "data_version")

    def __init__(self, panel, day, upto):
        from portfolio.context import PanelView

        self.date = day
        self.panel = PanelView(panel, upto)
        self.account = EmptyAccount()
        self.params = {}
        self.run_seed = day.toordinal()
        self.gateway = None
        self.data_version = 0


def permutation_p_value(spread: float | None, random_spreads: list[float]) -> float | None:
    """置换对照的经验 p 值（|随机利差| ≥ |实际利差| 的比例）。

    （要求可直测）：`spread` 为 NaN 时**必须**记 None——
    旧实现只判 ``is not None``，`np.abs(random) >= nan` 全 False → p=0.0
    （全族最显著），被送进课题内 BH-FDR 抬高 still_significant。
    """
    import numpy as np

    if spread is None or not np.isfinite(spread) or not random_spreads:
        return None
    rand = np.asarray(random_spreads, dtype=float)
    b = float(np.sum(np.abs(rand) >= abs(spread)))
    # R24A-F2：加 (b+1)/(B+1) 下限——此前用 b/B，p=0.0 完全合法（B=200 时
    # 分辨率 0.005 且"0 显著"会被 BH 当成最强证据）；教科书口径的下限保证
    # p>0 且随 B 单调收紧（200 次置换的 p=0 → 1/201）。
    return float((b + 1.0) / (len(rand) + 1.0))


def _feature_matrix(panel, feature: str) -> np.ndarray:
    """(T,N) 特征矩阵（因果口径，当日值只用截至当日数据）。"""
    close = panel.data["close"]
    if feature == "momentum_20":
        prev = np.vstack([np.full((20, close.shape[1]), np.nan), close[:-20]])
        with np.errstate(all="ignore"):
            return close / prev - 1.0
    if feature == "atr_pct":
        from core.indicators import atr as core_atr

        out = np.full(panel.shape, np.nan)
        for col in range(panel.shape[1]):
            df = pd.DataFrame({
                "high": panel.data["high"][:, col],
                "low": panel.data["low"][:, col],
                "close": close[:, col],
            })
            a = core_atr(df, 20)
            with np.errstate(all="ignore"):
                out[:, col] = a.to_numpy(dtype=float) / close[:, col]
        return out
    if feature == "er_10":
        out = np.full(panel.shape, np.nan)
        for col in range(panel.shape[1]):
            ser = pd.Series(close[:, col])
            er = efficiency_ratio(ser, 10).to_numpy(dtype=float)
            # F5（R3A）：core 的 efficiency_ratio 对 warmup/缺口行 fillna(0)
            # ——伪 0 值会把 IPO/复牌标的伪装成"完美无趋势"落最低桶。
            # 此处以"最近 11 行 close 全有限"为有效性掩码（ER(10) 需要
            # t 与 t−10 两端及路径完整），无效行恢复 NaN。
            valid = (
                pd.Series(np.isfinite(close[:, col]))
                .rolling(11, min_periods=11)
                .sum()
                .to_numpy(dtype=float)
                >= 11
            )
            out[~valid, col] = np.nan
            out[:, col] = np.where(valid, er, np.nan)
        return out
    raise ValueError(f"unknown feature: {feature} (supported: {FEATURES})")


def _spec_errors(spec: dict, ctx: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    ref, _ = parse_event_spec(spec.get("signal_module"))
    if not ref:
        errors.append("spec.signal_module is required (signal module ref)")
    else:
        registry = ctx.get("registry")
        if registry is not None and not registry.has(ref, slot="signal"):
            errors.append(f"signal module not registered: {ref}")
    feature = str(spec.get("feature") or "")
    if feature not in FEATURES:
        errors.append(f"spec.feature must be one of {FEATURES}, got {feature!r}")
    buckets = int(spec.get("buckets", 5) or 5)
    if buckets < 2 or buckets > 10:
        errors.append("spec.buckets must be 2..10")
    if spec.get("expect", "positive") not in ("positive", "negative"):
        errors.append("spec.expect must be positive|negative")
    # universe 非法值入口拦截：runner 里 resolve_universe_symbols
    # 才炸会把可防的 spec 错误变成 failed 实验入表（污染研究线+DSR 计数）
    uni = spec.get("universe")
    if uni is not None and uni != "liquidity_default":
        ok = (
            (isinstance(uni, str) and uni.startswith("single(") and uni.endswith(")")
             and uni[7:-1].strip())
            or isinstance(uni, (list, tuple))
        )
        if not ok:
            errors.append(
                f"spec.universe must be liquidity_default|single(SYMBOL)|[symbols], got {uni!r}"
            )
    return errors


def _subject_key(spec: dict) -> str:
    ref, _ = parse_event_spec((spec or {}).get("signal_module"))
    return f"{ref}:{(spec or {}).get('feature', '')}"


def run_bucket_analysis(db, experiment: dict, ctx: dict) -> dict:
    from portfolio.slots import ensure_builtins  # noqa: F401
    from research import holdout

    registry = ctx["registry"]
    spec = json.loads(experiment["spec_json"]) if isinstance(experiment["spec_json"], str) else experiment["spec_json"]
    signal_ref, signal_params = parse_event_spec(spec.get("signal_module"))
    feature = str(spec.get("feature"))
    n_buckets = int(spec.get("buckets", 5))
    horizons = [int(h) for h in spec.get("horizons", [10, 20])]
    primary_h = horizons[0]
    expect = str(spec.get("expect", "positive"))

    windows = holdout.get_windows(db)
    window = spec.get("window") or [windows["sample_start"], windows["sample_end"]]
    start, end = str(window[0]), str(window[1])
    touched = holdout.check_window(
        db, start=start, end=end, experiment_id=experiment["id"],
        token_id=(ctx or {}).get("holdout_token"),
    )

    symbols = resolve_universe_symbols(db, spec.get("universe"))
    liquidity_default = spec.get("universe") in (None, "liquidity_default")
    panel_warnings: list[str] = []
    panel = load_eval_panel(
        db, symbols=symbols, start=start, end=end, experiment_id=experiment["id"],
        min_amount20=DEFAULT_MIN_AMOUNT20 if liquidity_default else None,
        warnings_out=panel_warnings,
    )
    spec_obj = registry.require(signal_ref, slot="signal")
    module = spec_obj.factory(signal_params)
    if hasattr(module, "prepare"):
        module.prepare(panel)
    # regime 标签（§6.6.3 的 single_regime 注记输入；与 event_study 同口径）
    # R24A-F4：不可用/部分未知时**显式告警**（此前 bucket 侧完全不可见）
    _regime_warnings: list[str] = []
    labels = regime_labels(panel, warnings_out=_regime_warnings)
    # 生产指标类信号（trend_score_cross 等）：经受限句柄取面板外数据——
    # 评审 A-R2：不接线的死模块会产出 0 事件、伪装成合法 inconclusive 进台账
    if hasattr(module, "prepare_with_gateway"):
        from gateway.service import Gateway

        _gw = Gateway(db)
        _bound = _gw.bind(
            as_of=datetime.combine(pd.Timestamp(end).date(), time(15, 0)),
            caller_layer="research", run_id=experiment["id"],
        )
        module.prepare_with_gateway(_bound, list(panel.symbols), start)
        _gw.flush_audit()

    start_day, end_day = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    max_h = max(horizons)
    feat = _feature_matrix(panel, feature)
    fwd = forward_returns(panel, horizons, start, end)

    # 事件日截面：信号日 × 标的 → (t, col) + 特征值
    members = [UniverseMember(symbol=s) for s in panel.symbols]

    events: list[tuple[int, int, float]] = []  # (t, col, feature_value)
    date_to_idx = {d: i for i, d in enumerate(panel.dates)}
    # (symbol, 事件日) 去重 + 特征值/前瞻收益均取**事件日**（K3-R2-残留-1：
    # 与 event.py 同构——候选有效期 valid_days>1 时同一信号多次报到只计一次，
    # 否则分桶样本被膨胀 ~valid_days 倍，单调性/利差口径失真）
    seen_events: set[tuple[str, str]] = set()
    for t, day in enumerate(panel.dates):
        if not (start_day <= day <= end_day):
            continue
        if t + max_h >= len(panel.dates):
            continue
        for ev in module.scan(BucketScanCtx(panel, day, t), members):
            if ev.kind != "entry":
                continue
            col = panel._symbol_index.get(ev.symbol)
            if col is None:
                continue
            ev_day = ev.date if isinstance(ev.date, type(day)) else pd.Timestamp(ev.date).date()
            ev_t = date_to_idx.get(ev_day)
            if ev_t is None or ev_t + max_h >= len(panel.dates):
                continue
            key = (ev.symbol, ev_day.isoformat())
            if key in seen_events:
                continue
            seen_events.add(key)
            f = feat[ev_t, col]
            if np.isfinite(f):
                events.append((ev_t, col, float(f)))

    # 分桶：按特征分位数等频分 M 组
    evidence: dict[str, Any] = {"feature": feature, "buckets": n_buckets,
                                 "p_value": None}
    warnings: list[str] = list(_regime_warnings)
    suggested = "inconclusive"
    bucket_table: list[dict] = []
    spread = None
    random_band = None
    monotonicity = None

    if len(events) >= n_buckets * 10:
        feat_vals = np.array([f for _, _, f in events])
        quantiles = np.quantile(feat_vals, np.linspace(0, 1, n_buckets + 1))
        bucket_of = np.clip(np.searchsorted(quantiles[1:-1], feat_vals, side="right"), 0, n_buckets - 1)

        matrix = fwd[primary_h]
        bucket_returns: dict[int, list[float]] = {b: [] for b in range(n_buckets)}
        for k, (t, col, _f) in enumerate(events):
            v = matrix[t, col]
            if np.isfinite(v):
                bucket_returns[bucket_of[k]].append(float(v))
        means = []
        empty_buckets = 0
        for b in range(n_buckets):
            vals = bucket_returns[b]
            if not vals:
                empty_buckets += 1  # 空桶可见化
            means.append(float(np.mean(vals)) if vals else np.nan)
            bucket_table.append({
                "bucket": b + 1,
                "n": len(vals),
                "mean_forward_ret": means[-1],
                "feature_range": [float(quantiles[b]), float(quantiles[b + 1])],
            })
        if empty_buckets:
            # 特征值大量并列时等频分桶出空桶 → means 含 NaN →
            # spread/单调性 NaN → 静默 inconclusive；必须警告点破原因
            warnings.append(
                f"empty_buckets({empty_buckets}/{n_buckets})：特征值并列导致等频分桶空组，"
                "spread/单调性不可计算——判定按 inconclusive 属边界效应而非无结论"
            )

        # 单调性：相邻组收益差方向与预期一致的占比。
        # R24A-F2b：空桶产生的 NaN 差此前被 `np.sign(NaN)==expect_sign`（False）
        # 静默计为"不一致"，而 spread 仍是有限数 → 可把结论写成 rejected（"倒挂"），
        # 与告警文案"不可计算→inconclusive"相反。这里只统计**有限**相邻差，
        # 并把可比差数如实报出；可比差不足 2 时单调性记 None（判定 inconclusive）。
        diffs = np.diff(means)
        finite_diffs = diffs[np.isfinite(diffs)]
        expect_sign = 1.0 if expect == "positive" else -1.0
        comparable_diffs = int(finite_diffs.size)
        if comparable_diffs >= 2:
            monotonicity = float(np.mean(np.sign(finite_diffs) == expect_sign))
        else:
            monotonicity = None
        evidence["n_comparable_diffs"] = comparable_diffs

        # R24A-F2b：任一端桶为空（means 含 NaN）时 spread 不可比 → None（inconclusive），
        # 不再报一个"由内部空桶拼出来的有限利差"
        if np.isfinite(means[0]) and np.isfinite(means[-1]):
            spread = means[-1] - means[0]  # Q5−Q1（expect=positive 语境）
            if expect == "negative":
                spread = means[0] - means[-1]
        else:
            spread = None

        # 随机对照：同批事件打乱分组重算利差（seeded）
        rng = np.random.default_rng(7)
        random_spreads = []
        ev_ret = np.array([matrix[t, col] for t, col, _f in events], dtype=float)
        # R24A-F2：B=200 的分辨率只有 0.005（p=0.03 vs 独立复算 0.0142 落在噪声内）、
        # 95 分位带偏高 7.5%。提到 2000（代价：bucket 单次评估多 ~1 秒）。
        for _ in range(2000):
            shuffled = rng.permutation(len(events))
            groups = np.array_split(shuffled, n_buckets)
            g_means = []
            for g in groups:
                vals = ev_ret[g]
                vals = vals[np.isfinite(vals)]
                g_means.append(np.mean(vals) if len(vals) else np.nan)
            if np.isfinite(g_means).all():
                random_spreads.append(float(g_means[-1] - g_means[0]))
        if random_spreads:
            random_band = float(np.percentile(np.abs(random_spreads), 95))
            # 经验 p 值（评审 DS-P2-3）：NaN spread 必须记 None
            evidence["p_value"] = permutation_p_value(spread, random_spreads)

        if spread is not None and np.isfinite(spread) and random_band is not None                 and monotonicity is not None:
            # R24A-F2/F3：判定门此前是"单调性 ≥0.8 且 |spread|>band95"——该联合
            # 判据的零效应假阳率实测 **0.31%**（名义 5%，严 16 倍，confirmed
            # 结构性难达），而进 BH 家族的 p 却是 5% 口径；同一份证据两个口径。
            # 这里统一到 p（同一置换分布）：
            #   confirmed ⇔ p < 0.05 且方向单调（≥0.8）
            #   rejected  ⇔ p < 0.05 且方向倒挂（≤0.2）
            p_val = evidence.get("p_value")
            gate_alpha = 0.05
            evidence["gate_alpha"] = gate_alpha
            if p_val is not None and p_val < gate_alpha:
                if monotonicity >= 0.8:
                    suggested = "confirmed"
                elif monotonicity <= 0.2:
                    suggested = "rejected"  # 倒挂（方向反了本身也是结论）

    # §6.6.3 五类注记对**全部**评估模块生效：
    # 重叠率 / top1% 日集中度 / 单 regime 此前只有 event_study 侧算，
    # bucket 结构性拿不到其中三类。与 event 共用 _common 的实现，
    # bucket 用同一个 benchmark（510500.SS）打 regime 标签。
    _overlap_ratio, _top_day_share = overlap_and_cluster_stats(
        events, max_h=max_h, event_days=[panel.dates[t] for t, _c, _f in events]
    )
    _regimes = {labels[t] for t, _c, _f in events} if events else set()
    # 这里必须是 extend 而不是重绑定——上面
    # 已 append 的 empty_buckets 等模块级告警不能被覆盖掉（旧实现
    # `warnings = collect_warnings(...)` 把空桶告警整条丢弃）。
    warnings.extend(collect_warnings(
        event_count=len(events),
        overlap_ratio=_overlap_ratio,
        top_day_share=_top_day_share,
        regimes=_regimes,
    ))
    warnings.extend(panel_warnings)  # F2：流动性过滤缩水进 evidence
    warnings.extend(long_window_annotations(start))

    evidence.update({
        "bucket_table": bucket_table,
        "monotonicity": monotonicity,
        "q_spread": spread,
        "random_band_abs95": random_band,
        "primary_horizon": primary_h,
    })

    report = {
        "spec": spec, "window": [start, end], "symbols": len(panel.symbols),
        "events": len(events), "evidence": evidence, "warnings": warnings,
    }
    return {
        "baseline": {"kind": "random_shuffle", "n_permutations": 200,
                     "abs95_spread": random_band},
        "evidence": evidence,
        "warnings": warnings,
        "report": report,
        "suggested_verdict": suggested,
        "runs": [{
            "engine_run_id": None, "window_start": start, "window_end": end,
            "window_kind": "holdout" if touched else "sample", "holdout_touched": touched,
        }],
    }


def build_module() -> EvaluationModule:
    return EvaluationModule(
        name="bucket_analysis", version=1,
        description="截面分桶排序力检验（对照=随机打乱）",
        validate_spec=_spec_errors,
        subject_key=_subject_key,
        runner=run_bucket_analysis,
    )


register_evaluation(build_module())
