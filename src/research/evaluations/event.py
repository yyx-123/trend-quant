"""`event_study@1` 评估模块（详设 §6.5.2）。

回答的问题形态：这个信号/事件有没有料；历史统计规律挖掘。
取证方式：事件扫描（可带 regime 条件过滤）→ 前瞻 N 日收益/胜率/路径统计
（恢复时间/MAE/MFE）/迁移矩阵，向量化。
对照：无条件收益分布（全标的全日，同窗口径）。

spec（模块参数空间的当前快照，非平台 schema）：
```
{event: "ma_cross@1" | {module: "ma_cross@1", params: {n: 20}},
 context_filter: {benchmark: "510500.SS", rule: "close_above_ma200"} | None,
 universe: "single(510300.SS)" | "liquidity_default" | [symbols],
 horizons: [5, 10, 20, 40],
 path_stats: ["time_to_recover", "mae", "mfe"],
 expect: "positive" | "negative",     # 机器可判的假设方向（hypothesis 是文本）
 primary_horizon: 10}
```

注意（详设 §6.5.2 末条）：event_study 的 confirmed 只表示"含信息"，
不代表"能赚钱"——那是 portfolio_backtest 实验的事（§6.1.3 路径）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from datetime import datetime, time

from portfolio.registry import parse_module_ref
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
    unconditional_baseline,
)
from research.evaluations.base import EvaluationModule, register_evaluation


def parse_event_spec(raw: Any) -> tuple[str, dict]:
    """事件定义："ma_cross@1" / {module, params} / "ma_cross@1(n=20)" 简写。"""
    if isinstance(raw, dict):
        return str(raw.get("module") or ""), dict(raw.get("params") or {})
    text = str(raw or "").strip()
    if "(" in text and text.endswith(")"):
        ref, _, args = text[:-1].partition("(")
        params: dict[str, Any] = {}
        for part in args.split(","):
            if not part.strip():
                continue
            k, _, v = part.partition("=")
            params[k.strip()] = _coerce(v.strip())
        return ref.strip(), params
    return text, {}


def _coerce(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v.strip('"').strip("'")


def _spec_errors(spec: dict, ctx: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    ref, _params = parse_event_spec(spec.get("event"))
    if not ref:
        errors.append("spec.event is required (signal module ref)")
    else:
        try:
            parse_module_ref(ref)
            registry = ctx.get("registry")
            if registry is not None and not registry.has(ref, slot="signal"):
                errors.append(f"event module not registered: {ref}")
        except ValueError as exc:
            errors.append(str(exc))
    horizons = spec.get("horizons") or []
    if not isinstance(horizons, list) or not horizons:
        errors.append("spec.horizons must be a non-empty list")
    elif any(int(h) <= 0 for h in horizons):
        errors.append("spec.horizons must be positive ints")
    # primary_horizon ∈ horizons：写错时 runner 静默取空 dict
    # → 判定永远 inconclusive 的"假 inconclusive"必须在入口拦住
    primary = spec.get("primary_horizon")
    if primary is not None and horizons and int(primary) not in [int(h) for h in horizons]:
        errors.append(f"spec.primary_horizon must be one of horizons {horizons}, got {primary}")
    if spec.get("expect", "positive") not in ("positive", "negative"):
        errors.append("spec.expect must be positive|negative")
    if spec.get("event_side", "entry") not in ("entry", "exit", "both"):
        errors.append("spec.event_side must be entry|exit|both")
    cf = spec.get("context_filter")
    if cf is not None:
        if not isinstance(cf, dict) or not cf.get("benchmark"):
            errors.append("spec.context_filter must be {benchmark, rule}")
        elif str(cf.get("rule", "")) not in ("close_above_ma200", "close_below_ma200"):
            errors.append("spec.context_filter.rule must be close_above_ma200|close_below_ma200")
    return errors


def _subject_key(spec: dict) -> str:
    ref, _ = parse_event_spec((spec or {}).get("event"))
    return ref


def run_event_study(db, experiment: dict, ctx: dict) -> dict:
    """评估运行器：平台调用（worker/同步路径共用）。返回 verdict 部件。"""
    from portfolio.slots import ensure_builtins  # noqa: F401  确保内置模块注册
    from research import holdout

    registry = ctx["registry"]
    import json

    spec = json.loads(experiment["spec_json"]) if isinstance(experiment["spec_json"], str) else experiment["spec_json"]
    event_ref, event_params = parse_event_spec(spec.get("event"))
    horizons = [int(h) for h in spec.get("horizons", [5, 10, 20])]
    primary_h = int(spec.get("primary_horizon", horizons[0]))
    expect = str(spec.get("expect", "positive"))
    # DS-P1-4：context_filter 是研究问题的定义（样本选择口径）——实现，不再静默忽略；
    # K3-P2-3：event_side 支持研究 exit 侧事件（"跌破 MA20"类研究）
    context_filter = spec.get("context_filter")  # {benchmark, rule} 或 None
    event_side = str(spec.get("event_side", "entry"))
    transition_matrix = bool(spec.get("transition_matrix", False))

    windows = holdout.get_windows(db)
    # `or` 形：spec.window 显式为 null 时 .get(key, default) 仍取
    # None → None[0] TypeError 转 failed；统一为与 backtest/bucket 同写法
    window = spec.get("window") or [windows["sample_start"], windows["sample_end"]]
    start, end = str(window[0]), str(window[1])
    touched = holdout.check_window(
        db, start=start, end=end, experiment_id=experiment["id"],
        token_id=(ctx or {}).get("holdout_token"),
    )

    symbols = resolve_universe_symbols(db, spec.get("universe"))
    liquidity_default = spec.get("universe") in (None, "liquidity_default")
    if context_filter and context_filter.get("benchmark"):
        bm = str(context_filter["benchmark"]).upper()
        if bm not in symbols:
            symbols = [*symbols, bm]
    panel_warnings: list[str] = []
    panel = load_eval_panel(
        db, symbols=symbols, start=start, end=end,
        experiment_id=experiment["id"],
        min_amount20=DEFAULT_MIN_AMOUNT20 if liquidity_default else None,
        warnings_out=panel_warnings,
    )

    # 事件扫描：signal 模块双重身份（§6.1.3）——同一份代码定义事件
    spec_obj = registry.require(event_ref, slot="signal")
    module = spec_obj.factory(event_params)
    if hasattr(module, "prepare"):
        module.prepare(panel)
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

    start_day = pd.Timestamp(start).date()
    end_day = pd.Timestamp(end).date()
    fwd = forward_returns(panel, horizons, start, end)
    _regime_warnings: list[str] = []
    labels = regime_labels(panel, warnings_out=_regime_warnings)

    # 逐日扫描事件（entry 事件即事件日）
    events: list[tuple[int, int]] = []  # (t_idx, symbol_col)
    event_days: list = []
    members = [UniverseMember(symbol=s) for s in panel.symbols]
    day_idx_list = [i for i, d in enumerate(panel.dates) if start_day <= d <= end_day]
    max_h = max(horizons)

    # 简易上下文：signal.scan 需要 ctx.panel/account——用最小桩（事件扫描
    # 不需要账户）。
    from portfolio.context import PanelView

    class _ScanCtx:
        __slots__ = ("account", "data_version", "date", "gateway", "panel", "params", "run_seed")

        def __init__(self, day, upto):
            self.date = day
            self.panel = PanelView(panel, upto)
            self.account = EmptyAccount()
            self.params = {}
            self.run_seed = day.toordinal()
            self.gateway = None
            self.data_version = 0

    # context_filter：benchmark 的 regime 判定序列（close vs SMA200）
    cf_mask = None
    if context_filter:
        bm = str(context_filter["benchmark"]).upper()
        rule = str(context_filter.get("rule", "close_above_ma200"))
        bm_col = panel._symbol_index.get(bm)
        if bm_col is None:
            raise ValueError(f"context_filter benchmark not in panel: {bm}")
        bm_close = panel.data["close"][:, bm_col]
        # F1（R3A）：gateway 对无数据标的保留全 NaN 列——bm_col 命中不代表
        # benchmark 有数据；全 NaN 时 cf_mask 全 False → 全部事件被静默剔除
        # 成"假 inconclusive"。真 fail-loud 在此。
        if not np.isfinite(bm_close).any():
            raise ValueError(
                f"context_filter benchmark {bm} has no data in window "
                f"(events would be silently dropped to a fake inconclusive)"
            )
        bm_ma = pd.Series(bm_close).rolling(200, min_periods=200).mean().to_numpy()
        with np.errstate(all="ignore"):
            above = bm_close > bm_ma
        if rule == "close_above_ma200":
            cf_mask = above
        elif rule == "close_below_ma200":
            cf_mask = ~above & np.isfinite(bm_ma)
        else:
            raise ValueError(f"unknown context_filter rule: {rule}")

    date_to_idx = {d: i for i, d in enumerate(panel.dates)}
    seen_events: set[tuple[str, str]] = set()  # (symbol, 事件日) 去重（K3-P2-2）
    for t in day_idx_list:
        day = panel.dates[t]
        if t + max_h >= len(panel.dates):
            continue  # 前瞻数据不足的事件不取
        scan_ctx = _ScanCtx(day, t)
        for ev in module.scan(scan_ctx, members):
            # event_side：entry（默认）/ exit / both——exit 事件承载"跌破"类研究
            if event_side == "entry" and ev.kind != "entry":
                continue
            if event_side == "exit" and ev.kind != "exit":
                continue
            col = panel._symbol_index.get(ev.symbol)
            if col is None:
                continue
            ev_day = ev.date if isinstance(ev.date, type(day)) else pd.Timestamp(ev.date).date()
            # 前瞻收益从**事件日**起算（不是 scan 日——候选有效期内多次报到不重复计）
            ev_t = date_to_idx.get(ev_day)
            if ev_t is None or ev_t + max_h >= len(panel.dates):
                continue
            if context_filter and not cf_mask[ev_t]:
                continue
            # F6（R3A）口径声明：去重键 (symbol, 事件日) 不含 kind——
            # event_side="both" 时同日 entry+exit 只计 1 次（同日前瞻收益相同，
            # 统计上无差）
            key = (ev.symbol, ev_day.isoformat())
            if key in seen_events:
                continue
            seen_events.add(key)
            events.append((ev_t, col))
            event_days.append(ev_day)

    if transition_matrix:
        raise NotImplementedError(
            "transition_matrix not implemented in event_study@1 "
            "(fail-loud：拆成多个实验分别研究，勿静默忽略)"
        )

    # 事件收益 vs 无条件分布
    baseline = unconditional_baseline(fwd, panel, start_day, end_day)
    per_h: dict[int, dict] = {}
    for h in horizons:
        matrix = fwd[h]
        ev_vals = np.array([matrix[t, c] for t, c in events], dtype=float)
        ev_vals = ev_vals[np.isfinite(ev_vals)]
        base = baseline.get(h, {})
        delta = float(np.mean(ev_vals) - base.get("mean", 0.0)) if len(ev_vals) else None
        per_h[h] = {
            "n_events": len(ev_vals),
            "event_mean": float(np.mean(ev_vals)) if len(ev_vals) else None,
            "event_median": float(np.median(ev_vals)) if len(ev_vals) else None,
            "event_win_rate": float(np.mean(ev_vals > 0)) if len(ev_vals) else None,
            "baseline_mean": base.get("mean"),
            "baseline_median": base.get("median"),
            "baseline_win_rate": base.get("win_rate"),
            "baseline_n": base.get("n"),
            "delta_mean": delta,
            "delta_win_rate": (
                float(np.mean(ev_vals > 0) - base.get("win_rate", 0.0)) if len(ev_vals) else None
            ),
        }

    # 路径统计（最大 horizon 内的 MAE/MFE/恢复时间）
    path = {}
    if spec.get("path_stats") and events:
        close = panel.data["close"]
        max_h_idx = min(max_h, len(panel.dates) - 1)
        maes, mfes, recovers = [], [], []
        for t, c in events:
            entry = close[t, c]
            window = close[t + 1: t + 1 + max_h_idx, c]
            window = window[np.isfinite(window)]
            if not np.isfinite(entry) or len(window) == 0:
                continue
            maes.append(float(window.min() / entry - 1.0))
            mfes.append(float(window.max() / entry - 1.0))
            recovered = np.nonzero(window >= entry)[0]
            recovers.append(float(recovered[0] + 1) if len(recovered) else np.nan)
        path = {
            "mae_median": float(np.nanmedian(maes)) if maes else None,
            "mfe_median": float(np.nanmedian(mfes)) if mfes else None,
            "time_to_recover_median": float(np.nanmedian(recovers)) if recovers else None,
            "recover_rate": float(np.mean(np.isfinite(recovers))) if recovers else None,
        }

    # regime 拆分
    regime_split: dict[str, dict] = {}
    if events:
        matrix_full = fwd[primary_h]
        _start_day, _end_day = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        in_window = np.array([_start_day <= d <= _end_day for d in panel.dates])
        for label in ("above", "below"):
            sel = [k for k, (t, _c) in enumerate(events) if labels[t] == label]
            if not sel:
                continue
            vals = np.array([matrix_full[t, c] for k, (t, c) in enumerate(events) if k in sel])
            vals = vals[np.isfinite(vals)]
            base = baseline.get(primary_h, {})
            # regime 匹配基线：对照必须取**同 regime 日**的无条件
            # 均值，否则该 regime 自身的漂移会被记成事件效应（实证：全局基线
            # 0.00344 vs regime 匹配 0.00449 → delta_mean 差 30%，regime 平均
            # 收益与全局反向时符号都会翻）。全局口径同时保留供阅读对照。
            regime_days = np.array(
                [bool(in_window[i]) and labels[i] == label
                 for i in range(len(panel.dates))]
            )
            sub = matrix_full[regime_days, :]
            sub = sub[np.isfinite(sub)]
            regime_base_mean = float(np.mean(sub)) if len(sub) else None
            regime_split[label] = {
                "n_events": len(vals),
                "delta_mean": (
                    float(np.mean(vals) - regime_base_mean)
                    if len(vals) and regime_base_mean is not None else None
                ),
                "baseline_kind": "regime_matched",
                "baseline_mean": regime_base_mean,
                "baseline_n": len(sub),
                "delta_mean_vs_global": (
                    float(np.mean(vals) - base.get("mean", 0.0)) if len(vals) else None
                ),
            }

    # 警告
    regimes_in_events = {labels[t] for t, _c in events} if events else set()
    # 重叠率/日集中度与 bucket_analysis 共用同一实现（收口）
    overlap_ratio, top_share = overlap_and_cluster_stats(
        events, max_h=max_h, event_days=event_days
    )
    warnings = collect_warnings(
        event_count=len(events),
        overlap_ratio=overlap_ratio,
        top_day_share=top_share,
        regimes=regimes_in_events,
    )
    warnings.extend(long_window_annotations(start))
    warnings.extend(panel_warnings)  # F2（R3A）：流动性过滤缩水进 evidence
    # R24A-F4：regime 不可用/部分未知的**成因**（基准被过滤剔除 / 基准行情缺口 /
    # 预热）——此前只有"预热不足"一种文案，把"基准不在面板"误报成预热问题
    warnings.extend(_regime_warnings)
    # regime 预热透明度（DS-复审-R2 §4-2）：SMA200 预热不足的窗口前段，
    # regime 标签为 unknown / 条件掩码为 False 的事件被排除——必须显式可见，
    # 不能静默丢样本
    n_unknown = sum(1 for t in day_idx_list if labels[t] == "unknown")
    if n_unknown:
        warnings.append(f"regime_warmup_unknown({n_unknown} 日 SMA200 预热不足)")
    if context_filter and cf_mask is not None:
        n_cf_excl = int(np.sum(~np.isfinite(bm_ma[day_idx_list]))) if day_idx_list else 0
        if n_cf_excl:
            warnings.append(f"context_filter_warmup_excluded({n_cf_excl} 日条件掩码不可用)")

    # 判定：主 horizon 的 delta_mean 方向 + bootstrap 噪声带（簇口径，见下）
    evidence = {
        "per_horizon": {str(h): per_h[h] for h in horizons},
        "path_stats": path,
        "regime_split": regime_split,
        "primary_horizon": primary_h,
        "noise_band": None,
        "p_value": None,
    }
    suggested = "inconclusive"
    primary = per_h.get(primary_h, {})
    if events and primary.get("delta_mean") is not None and len(events) >= 30:
        matrix = fwd[primary_h]
        # R24A-F1（P1）：噪声带与 p 值必须按**事件日簇**重抽样。此前是事件级 iid
        # 重抽样（把 7393 个事件当独立样本），而事件按日成簇（同日均值 ACF(lag1)=0.42）
        # 且 80% 前瞻窗口重叠 → 均值 SE 低估 1.77×（10 日区块口径 3.29×），
        # 零效应下名义 5% 的实际拒绝率 17.0%（4000 次 MC；解析设计效应 3.18 吻合）。
        # 后果：delta_mean∈(0.19%,0.63%) 的前瞻效应会被报"显著"，而真实 p>0.05。
        pair_vals = [(t, c) for t, c in events]
        ev_vals = np.array([matrix[t, c] for t, c in pair_vals], dtype=float)
        ev_days = np.array([t for t, _c in pair_vals])
        finite_keep = np.isfinite(ev_vals)
        ev_vals_f = ev_vals[finite_keep]
        ev_days_f = ev_days[finite_keep]
        from research.evaluations._common import (
            _bootstrap_means,
            cluster_bootstrap_means,
        )

        # R25A-F1：块长 = 主 horizon（前瞻窗口跨度），把跨日重叠保留在区块内
        boot_means = cluster_bootstrap_means(
            ev_vals_f, ev_days_f, seed=7, block_days=max(int(primary_h), 1)
        )
        if boot_means is None:      # 单事件日等退化情形 → 退回 iid 并如实告警
            boot_means = _bootstrap_means(ev_vals_f, seed=7)
            warnings.append(
                "cluster_bootstrap_unavailable(事件日不足两个，退回事件级 iid 重抽样"
                "——重叠/成簇下的显著性是乐观的)"
            )
        band = (
            {"low": float(np.percentile(boot_means, 2.5)),
             "high": float(np.percentile(boot_means, 97.5))}
            if boot_means is not None and len(boot_means)
            else {"low": np.nan, "high": np.nan}
        )
        evidence["noise_band"] = band
        base_mean = baseline.get(primary_h, {}).get("mean", 0.0)
        # 经验 p 值（评审 DS-P2-3：bootstrap 均值越过基线均值的比例，供课题内
        # FDR 家族校正）——与噪声带同一簇分布
        if boot_means is not None and len(boot_means):
            p_emp = float(np.mean(
                boot_means <= base_mean if expect == "positive"
                else boot_means >= base_mean
            ))
            evidence["p_value"] = max(p_emp, 0.5 / len(boot_means))
        # 设计效应与簇规模如实落库（供读者判断"名义水平是否可信"）
        n_event_days = int(np.unique(ev_days_f).size) if ev_days_f.size else 0
        design_effect = None
        if n_event_days > 0 and ev_days_f.size > 0:
            per_day = ev_days_f.size / n_event_days
            if per_day > 0 and ev_vals_f.size > 1 and n_event_days > 1:
                iid_var = float(np.var(ev_vals_f, ddof=1) / ev_vals_f.size)
                clu_var = float(np.var(boot_means))
                design_effect = (clu_var / iid_var) if iid_var > 0 else None
        evidence["clustering"] = {
            "n_events": int(ev_vals_f.size),
            "n_event_days": n_event_days,
            "events_per_day": (round(ev_vals_f.size / n_event_days, 3) if n_event_days else None),
            "design_effect": (round(float(design_effect), 3) if design_effect else None),
            "bootstrap": "cluster_by_event_day",
        }
        # R25A-F1：簇太少时（<10 个事件日）区块重抽无从体现跨日结构 →
        # 不出判定（如实告警），避免 1~5 簇下 53%~86% 的假显著
        _n_days_primary = int(np.unique(ev_days_f).size) if ev_days_f.size else 0
        _min_days = 10
        if _n_days_primary < _min_days:
            warnings.append(
                f"clusters_insufficient({_n_days_primary} 个事件日 < {_min_days}："
                "区块重抽无法反映跨日重叠结构 → 本次不出判定)"
            )
        significant = bool(
            _n_days_primary >= _min_days
            and np.isfinite(band["low"])
            and (band["low"] > base_mean or band["high"] < base_mean)
        )
        direction_ok = (primary["delta_mean"] > 0) == (expect == "positive")
        if significant and direction_ok:
            suggested = "confirmed"
        elif significant and not direction_ok:
            suggested = "rejected"


    report = {
        "spec": spec, "event_module": event_ref, "event_params": event_params,
        "window": [start, end], "symbols": len(panel.symbols),
        "events": len(events), "evidence": evidence, "warnings": warnings,
    }
    return {
        "baseline": {"kind": "unconditional", "window": [start, end],
                     "per_horizon": {str(h): baseline[h] for h in horizons}},
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
        name="event_study", version=1,
        description="事件前瞻收益 vs 无条件分布（信号有没有料）",
        validate_spec=_spec_errors,
        subject_key=_subject_key,
        runner=run_event_study,
    )


register_evaluation(build_module())
