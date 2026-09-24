"""`portfolio_backtest@1` 评估模块（详设 §6.1.2/§6.6.1）。

回答的问题形态：机制类（止损/仓位/风控/排序/执行）。
取证方式：基准策略 + 插槽 diff → 调 L3 组合回测（基准同窗重跑由平台
执行，不接受创建者自报的对照数字，§6.6.2）。
对照：基准策略，Δ指标（regime 拆分：基准 SMA200 上/下 + breadth 分段）。

高原补跑（§6.5.1）与 DSR/PSR 判定（§6.6.5）在阶段 5 的 verdict 流水线
接线；本模块的 runner 负责：解析 → 双跑 → 差值与 regime 明细 → runs 部件。
"""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from research.evaluations._common import long_window_annotations
from research.evaluations.base import EvaluationModule, register_evaluation

_VERSION_REF = re.compile(r"^[^@\s]+@[1-9]\d*$")

# 空白基准前缀：base == blank-base@N 即创建型实验（详设 §6.1.4）——
# 七槽全填合法、免 compound 降档、对照自动切到 benchmark 阶梯。
BLANK_BASE_PREFIX = "blank-base@"


def _spec_errors(spec: dict, ctx: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]

    base = str(spec.get("base") or "").strip()
    if not base:
        errors.append("spec.base is required (strategy version ref)")
    elif not _VERSION_REF.match(base):
        errors.append(f"spec.base must be an immutable version ref <id>@<version>, got {base!r}")
    else:
        from portfolio.library import get_version

        db = ctx.get("db")
        if db is not None and get_version(db, base) is None:
            errors.append(f"spec.base version not in library: {base}")

    diff = spec.get("diff")
    if not isinstance(diff, list) or not diff:
        errors.append("spec.diff must be a non-empty list")
        diff = []

    from portfolio.registry import SEVEN_SLOTS

    seen_slots: list[str] = []
    module_refs: list[tuple[str, str]] = []  # (slot, ref)——同名元模块按槽消歧
    for i, item in enumerate(diff):
        if not isinstance(item, dict):
            errors.append(f"diff[{i}] must be a mapping")
            continue
        slot = str(item.get("slot") or "")
        if slot not in SEVEN_SLOTS:
            errors.append(f"diff[{i}]: unknown slot {slot!r}")
            continue
        seen_slots.append(slot)
        to = item.get("to")
        refs = to if isinstance(to, list) else [to]
        for ref in refs:
            if isinstance(ref, dict):
                ref = ref.get("module")
            if ref in (None, "", "none"):
                continue
            module_refs.append((slot, str(ref)))

    registry = ctx.get("registry")
    for slot, ref in module_refs:
        try:
            from portfolio.registry import parse_module_ref

            parse_module_ref(ref)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if registry is not None and not registry.has(ref, slot=slot):
            errors.append(f"module not registered (or not reviewed): {ref}")

    # 参数域校验（骨架 4 的"参数域内"腿，评审 DS-P2-4：非法参数不得先入账
    # 再失败——污染台账失败记录与 DSR 的试验计数）
    if registry is not None:
        from portfolio.registry import validate_params

        for item in diff:
            if not isinstance(item, dict):
                continue
            slot = str(item.get("slot") or "")
            to = item.get("to")
            refs = to if isinstance(to, list) else [to]
            for ref in refs:
                if isinstance(ref, dict):
                    ref_str, prm = str(ref.get("module") or ""), ref.get("params")
                else:
                    ref_str, prm = str(ref or ""), item.get("params")
                if ref_str in (None, "", "none", "None"):
                    continue
                spec_obj = registry.get(ref_str, slot=slot)
                if spec_obj is not None:
                    _n, perrs = validate_params(spec_obj.params_schema, prm)
                    errors.extend(f"diff[{slot}]: {e}" for e in perrs)

    # diff.from 与 base 实际模块一致性（评审 DS-P2-4：写错来源的单变量归因是假的）
    if registry is not None and not base.startswith(BLANK_BASE_PREFIX):
        db = ctx.get("db")
        if db is not None and not errors:
            from portfolio.library import get_version
            from portfolio.strategy import parse_strategy_yaml

            base_row = get_version(db, base)
            if base_row is not None:
                base_cfg = parse_strategy_yaml(base_row["config_yaml"], registry)
                for item in diff:
                    if not isinstance(item, dict):
                        continue
                    frm = item.get("from")
                    if frm in (None, "", "none"):
                        continue
                    slot = str(item.get("slot") or "")
                    if slot == "portfolio_risk":
                        actual = {g.module for g in base_cfg.gates}
                    elif slot in base_cfg.slots:
                        actual = {base_cfg.slots[slot].module}
                    else:
                        continue
                    frm_str = str(frm)
                    if frm_str != "none" and frm_str not in actual:
                        errors.append(
                            f"diff[{slot}].from={frm_str} does not match base config "
                            f"({sorted(m for m in actual if m)})"
                        )

    # 单变量纪律（骨架 3）：改进型实验一次只动一个插槽；多槽须显式
    # compound + 原因（串行实验链优先的提示照出）；创建型（blank-base）
    # 自动识别、豁免降档，但要求七槽全填（详设 §6.1.4）。
    is_creation = base.startswith(BLANK_BASE_PREFIX)
    if is_creation:
        missing = [s for s in SEVEN_SLOTS if s not in seen_slots]
        if missing:
            errors.append(f"creation-type experiment must fill all seven slots, missing: {missing}")
        elif not module_refs:
            # 七槽全 none 的"创建型"是空实验（评审 DS-P3-1）——不产生任何
            # 交易的策略没有可回答的问题
            errors.append("creation-type experiment must bind at least one real module (all-none is empty)")
    elif len(seen_slots) > 1:
        if not spec.get("is_compound") or not str(spec.get("compound_reason") or "").strip():
            errors.append(
                "multi-slot diff requires is_compound=true with compound_reason "
                "(prefer a serial experiment chain)"
            )

    window_mode = str(spec.get("window_mode") or "static_holdout")
    if window_mode not in ("static_holdout", "walk_forward"):
        errors.append(f"unknown window_mode: {window_mode}")
    return errors


def _subject_key(spec: dict) -> str:
    base = str((spec or {}).get("base") or "")
    return base.split("@", 1)[0] if base else ""


# ----------------------------------------------------------------------
# 运行器（L4 → L3 服务面：resolve + 双跑 + 差值）
# ----------------------------------------------------------------------

_DELTA_METRICS = ("annual_return", "max_drawdown", "sharpe", "sortino", "calmar", "turnover")


def _nav_summary(nav_rows: list[dict], trades: list[dict] | None = None) -> dict:
    """组合 NAV 摘要。turnover 必须来自真实成交（评审 DS-P1-3：恒 0 假证据已修）——
    trades 为回测器内存成交清单（{price, qty}）；None 时 turnover 记 None
    （walk-forward 拼接路径无逐段成交，显式标注不可用）。"""
    from rule_backtest.metrics import compute_summary

    turnover_total = _trades_turnover_total(trades) if trades is not None else None
    summary = compute_summary(nav_rows, trades=[], turnover_total=turnover_total or 0.0)
    if turnover_total is None:
        summary["turnover"] = None
    return summary


def _trades_turnover_total(trades: list[dict]) -> float:
    """Σ|price × qty|（成交总额；turnover = 总额 ÷ 平均权益，由 compute_summary 算）。"""
    return sum(abs(float(t["price"]) * int(t["qty"])) for t in trades)


def _segment_metrics(nav_rows: list[dict]) -> dict:
    """一段日 NAV 的段内指标（年化/Sharpe）。用于连续段（walk-forward 折段）。"""
    import numpy as np

    eq = [float(r["equity"]) for r in nav_rows if r.get("equity") is not None]
    if len(eq) < 2:
        return {}
    rets = np.diff(eq) / np.asarray(eq[:-1])
    std = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    total = eq[-1] / eq[0] - 1.0
    n = len(rets)
    annual = (1.0 + total) ** (252.0 / n) - 1.0 if total > -1 and n > 0 else -1.0
    return {
        "annual_return": float(annual),
        "sharpe": float(rets.mean() / std * np.sqrt(252)) if std > 0 else 0.0,
        "n_days": int(n),
    }


def _regime_segment_metrics(nav_rows: list[dict], days: set) -> dict:
    """regime 段指标：日收益在全序列上算好（相邻日差分），再按 regime 日筛——
    避免非连续净值直接差分的失真（评审 B-P2）。"""
    import numpy as np

    rows = [(pd.Timestamp(r["date"]).date(), float(r["equity"]))
            for r in nav_rows if r.get("equity") is not None]
    if len(rows) < 2:
        return {}
    eq = np.array([e for _, e in rows])
    rets = np.diff(eq) / eq[:-1]
    ret_days = [rows[k + 1][0] for k in range(len(rows) - 1)]  # 收益归属后一日
    sel = [r for r, d in zip(rets, ret_days) if d in days]
    if len(sel) < 5:
        return {}
    arr = np.asarray(sel)
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    sharpe = float(arr.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    annual = float(arr.mean() * 252)
    return {"annual_return": annual, "sharpe": sharpe, "n_days": len(arr)}


def _regime_split(exp_nav: list[dict], base_nav: list[dict], bench_nav: list[dict] | None) -> dict:
    """regime 拆分：基准（沪深300 benchmark 策略 NAV 代理）SMA200 上/下两段的
    Δ年化/ΔSharpe（breadth 分段需要 universe 面板，数据线二期补）。"""
    if not bench_nav:
        return {}
    bench = pd.DataFrame(bench_nav)
    bench["date"] = pd.to_datetime(bench["date"]).dt.date
    bench["close"] = pd.to_numeric(bench["equity"], errors="coerce")
    bench["ma200"] = bench["close"].rolling(200, min_periods=200).mean()
    regime_by_day = {
        r["date"]: ("above" if r["close"] > r["ma200"] else "below")
        for r in bench.dropna(subset=["ma200"])[["date", "close", "ma200"]].to_dict("records")
    }
    out: dict[str, dict] = {}
    for label in ("above", "below"):
        days = {d for d, v in regime_by_day.items() if v == label}
        if not days:
            continue
        m_exp = _regime_segment_metrics(exp_nav, days)
        m_base = _regime_segment_metrics(base_nav, days)
        if not m_exp or not m_base:
            continue
        out[label] = {
            "n_days": m_exp["n_days"],
            "delta_annual_return": m_exp["annual_return"] - m_base["annual_return"],
            "delta_sharpe": m_exp["sharpe"] - m_base["sharpe"],
        }
    return out


def run_portfolio_backtest(db, experiment: dict, ctx: dict) -> dict:
    """评估运行器：基准同窗重跑 + 实验跑 → Δ 指标明细（完整报告，不摘要化）。"""
    from portfolio import service as portfolio_service
    from portfolio.library import latest_version, require_version
    from portfolio.seed import seed_default_library
    from portfolio.slots import REGISTRY, ensure_builtins
    from research import holdout

    registry = (ctx or {}).get("registry") or REGISTRY
    ensure_builtins()
    seed_default_library(db, registry)  # 幂等：benchmark/blank-base 总在库

    spec = json.loads(experiment["spec_json"]) if isinstance(experiment["spec_json"], str) else experiment["spec_json"]
    base_ref = str(spec["base"])
    diff = spec.get("diff") or []

    config, resolved_yaml = portfolio_service.resolve_experiment_config(
        db, base_version_id=base_ref, diff=diff, registry=registry,
        new_name=f"exp-{experiment['id']}",
    )

    windows = holdout.get_windows(db)
    window = spec.get("window") or [windows["sample_start"], windows["sample_end"]]
    start, end = str(window[0]), str(window[1])
    touched = holdout.check_window(
        db, start=start, end=end, experiment_id=experiment["id"],
        token_id=(ctx or {}).get("holdout_token"),
    )
    window_kind = "holdout" if touched else "sample"
    window_mode = str(spec.get("window_mode") or "static_holdout")
    wf_info = None

    is_creation = base_ref.startswith(BLANK_BASE_PREFIX)
    run_params = {
        "window": [start, end],
        "window_kind": window_kind,
        "initial_capital": float(spec.get("initial_capital", 1_000_000)),
    }
    if window_mode == "walk_forward":
        # 滚动样本外（审计 F 项接纳，阶段 5）：窗口切 n_folds 个滚动 OOS 段，
        # 每段独立跑实验+基准（每段独立初始资金），deltas 逐段计算后拼接。
        runs_out: list[dict] = []
        n_folds = int(spec.get("n_folds", 4) or 4)
        fold_days = pd.bdate_range(start, end)
        if len(fold_days) < n_folds * 20:
            raise ValueError("walk_forward window too short for n_folds")
        fold_edges = np.array_split(np.arange(len(fold_days)), n_folds)
        wf_folds = []
        stitched_exp_rets: list[float] = []
        stitched_base_rets: list[float] = []
        stitched_dates: list[str] = []
        for k, idxs in enumerate(fold_edges):
            f_start = str(fold_days[idxs[0]].date())
            f_end = str(fold_days[idxs[-1]].date())
            f_params = {**run_params, "window": [f_start, f_end]}
            exp_r = portfolio_service.run_backtest(
                db, config=config, registry=registry, run_params=f_params,
                strategy_ref=f"experiment:{experiment['id']}:wf{k}",
            )
            runs_out.append({
                "engine_run_id": exp_r["run_id"], "window_start": f_start,
                "window_end": f_end, "window_kind": window_kind, "holdout_touched": touched,
            })
            base_r = None
            if not is_creation:
                from portfolio.library import require_version
                from portfolio.strategy import parse_strategy_yaml

                base_cfg = parse_strategy_yaml(require_version(db, base_ref)["config_yaml"], registry)
                base_r = portfolio_service.run_backtest(
                    db, config=base_cfg, registry=registry, run_params=f_params,
                    strategy_ref=base_ref,
                )
            else:
                from portfolio.library import latest_version
                from portfolio.strategy import parse_strategy_yaml

                bench_row = latest_version(db, "bench-buy-hold-csi300")
                bench_cfg = parse_strategy_yaml(bench_row["config_yaml"], registry)
                base_r = portfolio_service.run_backtest(
                    db, config=bench_cfg, registry=registry, run_params=f_params,
                    strategy_ref=bench_row["id"],
                )
            runs_out.append({
                "engine_run_id": base_r["run_id"], "window_start": f_start,
                "window_end": f_end, "window_kind": window_kind, "holdout_touched": touched,
            })
            m_e = _nav_summary(exp_r["daily_nav"])
            m_b = _nav_summary(base_r["daily_nav"])
            wf_folds.append({
                "fold": k, "window": [f_start, f_end],
                "delta_sharpe": float(m_e.get("sharpe", 0.0) - m_b.get("sharpe", 0.0)),
                "delta_annual_return": float(m_e.get("annual_return", 0.0) - m_b.get("annual_return", 0.0)),
            })
            er = _safe_daily_rets(exp_r["daily_nav"])
            br = _safe_daily_rets(base_r["daily_nav"])
            stitched_exp_rets.extend(er)
            stitched_base_rets.extend(br)
            # 拼接序列保留真实日期（评审 DS-P1-3："oos-i" 占位标签会被
            # pd.to_datetime 解析炸掉——收益归属后一日，取 fold 内日期）
            fold_dates = [str(r["date"]) for r in exp_r["daily_nav"][1:]]
            stitched_dates.extend(fold_dates)
        # 拼接 OOS 序列作为汇总口径
        exp_eq = np.cumprod(1.0 + np.asarray(stitched_exp_rets))
        base_eq = np.cumprod(1.0 + np.asarray(stitched_base_rets))
        stitched_nav = [
            {"date": stitched_dates[i], "equity": float(v)} for i, v in enumerate(exp_eq)
        ]
        stitched_base_nav = [
            {"date": stitched_dates[i], "equity": float(v)} for i, v in enumerate(base_eq)
        ]
        wf_info = {"n_folds": n_folds, "folds": wf_folds}
        # trades=None（不是空列表）：walk-forward 拼接无逐笔成交，turnover
        # 显式不可用——空列表会让 experiment_summary.turnover 假 0（DS-R2 残留）
        exp_result = {"run_id": None, "daily_nav": stitched_nav, "trades": None, "unfilled": []}
        base_nav = stitched_base_nav
        base_summary = _nav_summary(stitched_base_nav)  # trades=None → turnover=None
        exp_summary = _nav_summary(stitched_nav)
        deltas = {}
        if base_summary:
            for m in _DELTA_METRICS:
                deltas[f"delta_{m}"] = (
                    None if m == "turnover"
                    else float(exp_summary.get(m, 0.0)) - float(base_summary.get(m, 0.0))
                )
        regime = {}
        return _assemble_result(
            db, experiment, spec, base_ref, resolved_yaml, is_creation,
            exp_result, base_nav, base_summary, deltas, regime, runs_out,
            start, end, window_kind, touched, window_mode, wf_info,
            run_params=run_params, registry=registry, config=config,
        )

    # 实验跑（对照是基准策略同窗重跑——平台执行，不接受自报）
    exp_result = portfolio_service.run_backtest(
        db, config=config, registry=registry, run_params=run_params,
        strategy_ref=f"experiment:{experiment['id']}",
    )
    runs_out = [{
        "engine_run_id": exp_result["run_id"], "window_start": start,
        "window_end": end, "window_kind": window_kind, "holdout_touched": touched,
    }]

    base_row = None
    base_summary = None
    base_nav: list[dict] = []
    if not is_creation:
        from portfolio.strategy import parse_strategy_yaml

        base_row = require_version(db, base_ref)
        base_cfg = parse_strategy_yaml(base_row["config_yaml"], registry)
        base_result = portfolio_service.run_backtest(
            db, config=base_cfg, registry=registry, run_params=run_params,
            strategy_ref=base_ref,
        )
        base_nav = base_result["daily_nav"]
        base_summary = _nav_summary(base_nav, base_result.get("trades"))
        runs_out.append({
            "engine_run_id": base_result["run_id"], "window_start": start,
            "window_end": end, "window_kind": window_kind, "holdout_touched": touched,
        })
    else:
        # 创建型：对照 = benchmark 阶梯同窗（取 buy-hold 为 deltas 基准）
        from portfolio.strategy import parse_strategy_yaml

        bench_row = latest_version(db, "bench-buy-hold-csi300")
        if bench_row is not None:
            base_row = bench_row
            bench_cfg = parse_strategy_yaml(bench_row["config_yaml"], registry)
            bench_result = portfolio_service.run_backtest(
                db, config=bench_cfg, registry=registry, run_params=run_params,
                strategy_ref=bench_row["id"],
            )
            base_nav = bench_result["daily_nav"]
            base_summary = _nav_summary(base_nav, bench_result.get("trades"))
            runs_out.append({
                "engine_run_id": bench_result["run_id"], "window_start": start,
                "window_end": end, "window_kind": window_kind, "holdout_touched": touched,
            })

    exp_summary = _nav_summary(exp_result["daily_nav"], exp_result.get("trades"))
    deltas = {}
    if base_summary:
        for m in _DELTA_METRICS:
            deltas[f"delta_{m}"] = (
                None
                if m == "turnover" and (exp_summary.get(m) is None or base_summary.get(m) is None)
                else float(exp_summary.get(m, 0.0)) - float(base_summary.get(m, 0.0))
            )

    # regime 拆分（基准沪深300 SMA200 上/下；breadth 分段待数据线二期）
    bench_nav_for_regime = None
    bench300 = latest_version(db, "bench-buy-hold-csi300")
    regime_warnings: list[str] = []
    if bench300 is not None:
        if base_row is not None and bench300["id"] == base_row["id"]:
            bench_nav_for_regime = base_nav
        else:
            from portfolio.backtester import BacktestError
            from portfolio.strategy import parse_strategy_yaml as _psy

            try:
                bench_cfg = _psy(bench300["config_yaml"], registry)
                bench_result = portfolio_service.run_backtest(
                    db, config=bench_cfg, registry=registry, run_params=run_params,
                    strategy_ref=bench300["id"],
                )
                bench_nav_for_regime = bench_result["daily_nav"]
                runs_out.append({
                    "engine_run_id": bench_result["run_id"], "window_start": start,
                    "window_end": end, "window_kind": window_kind, "holdout_touched": touched,
                })
            except BacktestError:
                # 基准无数据（如测试库不含 510300）→ regime 拆分降级为空 + 警告
                regime_warnings.append("regime_benchmark_unavailable(沪深300 无数据)")
    # GLM53F-P2-3：基准不可用时**禁用** regime 拆分——不得回退用实验基准策略
    # 自身 NAV 做 SMA200 分段（那是"按基准策略净值趋势分段"的假证据，会直接
    # 喂给 regime 塌陷检查）
    regime = _regime_split(exp_result["daily_nav"], base_nav, bench_nav_for_regime)
    return _assemble_result(
        db, experiment, spec, base_ref, resolved_yaml, is_creation,
        exp_result, base_nav, base_summary, deltas, regime, runs_out,
        start, end, window_kind, touched, window_mode, wf_info,
        run_params=run_params, registry=registry, config=config,
        extra_warnings=regime_warnings,
    )


def _assemble_result(db, experiment, spec, base_ref, resolved_yaml, is_creation,
                     exp_result, base_nav, base_summary, deltas, regime, runs_out,
                     start, end, window_kind, touched, window_mode, wf_info,
                     *, run_params, registry, config, extra_warnings=()) -> dict:
    """证据组装 + 判定器全家桶（主路径与 walk_forward 拼接路径共用）。"""
    from portfolio import service as portfolio_service

    base_summary = base_summary or {}
    # 成本明细（换手与费用：判定建议的"换手增幅成本可解释"输入）
    # 分层铁律：L4 不跨级 import L2——经 L3 服务面转发（评审 A-P1-2）
    exp_summary = _nav_summary(exp_result["daily_nav"], exp_result.get("trades"))
    exp_fills = (
        portfolio_service.load_fills(db, exp_result["run_id"])
        if exp_result.get("run_id") else []
    )
    fee_total = sum(float(f["fee_total"]) for f in exp_fills)

    # ---- 判定器全家桶（§6.5.1/§6.6.5；golden 对拍见 tests/unit/test_research_stats.py）
    from portfolio.reports import daily_returns as _daily_returns
    from portfolio.reports import pair_round_trips
    from research.stats.bootstrap import sharpe_block_bootstrap, trade_bootstrap_bands
    from research.stats.paired import paired_sharpe_comparison
    from research.stats.psr import dsr as _dsr
    from research.stats.psr import mintrl as _mintrl
    from research.stats.psr import moments as _moments
    from research.stats.psr import psr as _psr
    from research.verdict_rules import load_rules, plateau_neighbors, plateau_verdict, suggest_backtest_verdict

    exp_rets_s = _daily_returns(exp_result["daily_nav"])
    base_rets_s = _daily_returns(base_nav) if base_nav else None
    exp_rets = exp_rets_s.dropna().to_numpy()
    base_rets = base_rets_s.dropna().to_numpy() if base_rets_s is not None else np.array([])
    mean_r, std_r, skew, kurt = _moments(exp_rets)
    sr_hat = float(mean_r / std_r) if std_r > 0 else 0.0
    sr_base = (
        float(np.mean(base_rets) / np.std(base_rets, ddof=1))
        if len(base_rets) > 1 and np.std(base_rets, ddof=1) > 0 else 0.0
    )
    attempt_index = int(experiment.get("attempt_index") or 1)
    # 配对检验（DS-P1-6）：改进型实验是高相关配对——差序列 Sharpe 带 +
    # 差序列 DSR 才是判定口径；单序列 PSR/DSR 保留为展示件。
    # DS-复审-R2 §4-3：配对前必须按**日期**取交集——各自 dropna 后的位置
    # 对齐在两侧长度不齐（基准腿缺几天）时会整体错位，差异静默进 t_stat
    paired = None
    if base_rets_s is not None and len(base_rets):
        joined = pd.concat(
            [exp_rets_s.rename("exp"), base_rets_s.rename("base")],
            axis=1, join="inner",
        ).dropna()
        paired = paired_sharpe_comparison(
            joined["exp"].to_numpy(), joined["base"].to_numpy(), n_trials=attempt_index
        )
        if paired is not None:
            paired["date_joined"] = True
            dropped = (len(exp_rets) - len(joined)) + (len(base_rets) - len(joined))
            if dropped > 0:
                paired["dates_dropped"] = int(dropped)
    # Sortino 推断（阶段 5 判定器全家桶）：同一 PSR 框架应用于下行偏差口径
    # ——下行风险调整后的显著性（Sharpe 口径的姊妹件，公式为近似沿用）。
    downside = exp_rets[exp_rets < 0]
    downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    sortino_hat = float(mean_r / downside_std) if downside_std > 0 else 0.0
    base_downside = base_rets[base_rets < 0] if len(base_rets) else np.array([])
    base_downside_std = float(np.std(base_downside, ddof=1)) if len(base_downside) > 1 else 0.0
    sortino_base = (
        float(np.mean(base_rets) / base_downside_std) if base_downside_std > 0 else 0.0
    )
    stats = {
        "paired": paired,
        "psr": _psr(sr_hat, sr_base, len(exp_rets), skew, kurt),
        "psr_sortino": _psr(sortino_hat, sortino_base, len(exp_rets), skew, kurt),
        "dsr": _dsr(sr_hat, len(exp_rets), skew, kurt, max(attempt_index, 1)),
        "mintrl_days": _mintrl(sr_hat, sr_base, skew, kurt),
        "sharpe_bootstrap": sharpe_block_bootstrap(exp_rets, seed=7),
        "attempt_index": attempt_index,
    }

    # MC 置信带（交易序列 bootstrap；阶段 5 审计 G 项接纳）
    rounds = pair_round_trips(exp_fills)
    pnls = [r["pnl_net"] for r in rounds]
    mc = trade_bootstrap_bands(
        pnls, initial_equity=float(spec.get("initial_capital", 1_000_000)), seed=7
    )

    # 参数高原自动补跑（§6.5.1）：diff 中改动的数值参数在邻域补跑
    plateau = None
    plateau_items = _plateau_items(spec.get("diff") or [])
    pbo_info = None
    if plateau_items and not is_creation:
        neighbor_deltas: list[float] = []
        probes: list[dict] = []
        for item in plateau_items:
            slot, param, selected = item["slot"], item["param"], item["value"]
            for neighbor in plateau_neighbors(param, float(selected)):
                probe_diff = _with_param(spec.get("diff") or [], slot, param, neighbor)
                probe_cfg, _yaml = portfolio_service.resolve_experiment_config(
                    db, base_version_id=base_ref, diff=probe_diff, registry=registry,
                    new_name=f"exp-{experiment['id']}-plateau-{param}-{neighbor}",
                )
                probe_result = portfolio_service.run_backtest(
                    db, config=probe_cfg, registry=registry,
                    run_params={**run_params, "window_kind": "plateau_probe"},
                    strategy_ref=f"experiment:{experiment['id']}:plateau",
                )
                probe_summary = _nav_summary(probe_result["daily_nav"])
                d = float(probe_summary.get("sharpe", 0.0)) - (
                    float(base_summary.get("sharpe", 0.0)) if base_summary else 0.0
                )
                neighbor_deltas.append(d)
                probes.append({"slot": slot, "param": param, "value": neighbor,
                               "delta_sharpe": d, "run_id": probe_result["run_id"]})
                runs_out.append({
                    "engine_run_id": probe_result["run_id"], "window_start": start,
                    "window_end": end, "window_kind": "plateau_probe",
                    "holdout_touched": touched,
                })
        selected_delta = (exp_summary.get("sharpe", 0.0) or 0.0) - (
            base_summary.get("sharpe", 0.0) if base_summary else 0.0
        )
        plateau = {
            **plateau_verdict(float(selected_delta), neighbor_deltas),
            "probes": probes,
        }
        # PBO（审计 B 项，阶段 5）：变体矩阵 = 主选 + 邻域探针的日收益，
        # CSCV 过拟合概率（变体数 ≥ 2 才有意义）
        try:
            from research.stats.fdr_pbo import pbo_cscv

            variant_rets = [_safe_daily_rets(exp_result["daily_nav"])]
            for probe in probes:
                probe_run = portfolio_service.load_nav(db, probe["run_id"])
                variant_rets.append(_safe_daily_rets(probe_run))
            min_len = min(len(r) for r in variant_rets)
            if len(variant_rets) >= 2 and min_len >= 60:
                matrix = np.column_stack([np.asarray(r[-min_len:]) for r in variant_rets])
                pbo_info = pbo_cscv(matrix, n_blocks=8)
        except Exception:
            pbo_info = None

    warnings: list[str] = list(extra_warnings)
    warnings.extend(exp_result.get("warnings") or [])  # 运行级告警（heat_cap 退化等）
    # 长窗口三注记（详设 §6.6.4，评审 DS-P2-5：进实验路径，不只进脚本产物；
    # DS-复审-R2 §4-1：共享件，event/bucket/distribution 同口径）
    warnings.extend(long_window_annotations(start))
    if touched:
        warnings.append("holdout_touched")
    warnings.append("survivorship_bias(universe 为当前池穿越历史)")
    if not exp_result["trades"]:
        warnings.append("no_trades(实验窗口内零成交)")
    if plateau is None and not is_creation:
        # GLM53F-P2-1③：换模块类 diff 无参数高原证据——不阻断 confirmed，
        # 但必须显式可见（ plateau 门对这类实验是缺席而非通过）
        warnings.append("plateau_evidence_absent(换模块类 diff 无高原证据，孤峰检查缺席)")
    elif plateau and plateau.get("verdict") == "peak":
        warnings.append("plateau_peak(参数孤峰，疑似过拟合)")
    # GLM53F-P2-1④：换手增幅成本可解释性（§6.5.1）——阈值化不合适，
    # 进警告由人工 confirm 判读
    _d_turn = deltas.get("delta_turnover")
    if _d_turn is not None and _d_turn > 0.5:
        warnings.append(f"turnover_jump(换手增幅 {_d_turn:+.0%}，成本可解释性须人工确认)")
    if stats["sharpe_bootstrap"].get("low") is not None and stats["sharpe_bootstrap"]["low"] < 0 < (stats["sharpe_bootstrap"]["point"] or 0):
        warnings.append("bootstrap_band_crosses_zero(Sharpe 置信带跨零)")

    evidence = {
        "deltas_vs_base": deltas,
        "experiment_summary": {k: exp_summary[k] for k in _DELTA_METRICS if k in exp_summary},
        "base_summary": {k: base_summary[k] for k in _DELTA_METRICS if k in base_summary} if base_summary else None,
        "regime_split": regime,
        "stats": stats,
        "mc_bands": mc,
        "fee_total": fee_total,
        "trades": (
            len(exp_result["trades"]) if exp_result.get("trades") is not None else None
        ),
        "unfilled_by_reason": _count_by_reason(exp_result["unfilled"]),
        "is_compound": bool(spec.get("is_compound")),
        "plateau": plateau,
        "pbo": pbo_info,
        "window_mode": window_mode,
    }
    if window_mode == "walk_forward":
        evidence["walk_forward"] = wf_info

    suggested = suggest_backtest_verdict(evidence, rules=load_rules(db))

    # 完整实验报告（§6.5.0 一个都不许摘要化；评审 DS-P1-1 死代码已接线）：
    # 全指标表 + regime 逐段 + 高原各点 + 交易/未成交/**风控拦截统计**（P2-4：
    # gate_log 不再被丢弃）+ round_trips + heat/exposure/槽位利用/成本拖累。
    full_run_report = None
    if exp_result.get("run_id"):
        from portfolio import reports as _reports

        _slot_limit = next(
            (int(g.params.get("max_positions")) for g in config.gates
             if g.module and g.module.startswith("slot_limit@")
             and g.params.get("max_positions")),
            None,
        )
        full_run_report = _reports.build_report(
            db,
            run_id=exp_result["run_id"],
            nav_rows=exp_result["daily_nav"],
            fills=exp_fills,
            unfilled=exp_result["unfilled"],
            gate_log=exp_result.get("gate_log", []),
            benchmarks={"base": base_nav} if base_nav else {},
            positions_snapshots=portfolio_service.load_positions(db, exp_result["run_id"]),
            slot_limit=_slot_limit,
            round_trips=exp_result.get("round_trips"),
        )
    report = {
        "spec": spec,
        "resolved_config_yaml": resolved_yaml,
        "window": [start, end],
        "engine_runs": [r["engine_run_id"] for r in runs_out],
        "evidence": evidence,
        "warnings": warnings,
        "full_run_report": full_run_report,
    }
    return {
        "baseline": {
            "kind": "blank-base" if is_creation else "base_strategy",
            "base": base_ref,
            "summary": {k: base_summary[k] for k in _DELTA_METRICS} if base_summary else None,
        },
        "evidence": evidence,
        "warnings": warnings,
        "report": report,
        "suggested_verdict": suggested,
        "runs": runs_out,
    }


def _plateau_items(diff: list[dict]) -> list[dict]:
    """diff 中"同模块、数值参数变化"的项（高原补跑对象）。"""
    out: list[dict] = []
    for item in diff:
        to = item.get("to")
        if isinstance(to, list) or to in (None, "", "none"):
            continue
        frm = item.get("from")
        params = item.get("params") or {}
        if frm and str(frm).split("@")[0] != str(to).split("@")[0]:
            continue  # 换模块不是参数扰动
        for key, value in params.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append({"slot": item["slot"], "param": key, "value": value})
    return out


def _with_param(diff: list[dict], slot: str, param: str, value) -> list[dict]:
    """diff 深拷贝并把 (slot, param) 替换为邻域值。"""
    import copy

    out = copy.deepcopy(diff)
    for item in out:
        if item.get("slot") == slot:
            item.setdefault("params", {})[param] = value
    return out




def _safe_daily_rets(nav_rows: list[dict]) -> list[float]:
    import numpy as np

    eq = [float(r["equity"]) for r in nav_rows if r.get("equity") is not None]
    if len(eq) < 2:
        return []
    rets = np.diff(eq) / np.asarray(eq[:-1])
    return [float(r) for r in rets]


def _count_by_reason(unfilled: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in unfilled:
        out[u["reason"]] = out.get(u["reason"], 0) + 1
    return out


def build_module(runner=None) -> EvaluationModule:
    return EvaluationModule(
        name="portfolio_backtest",
        version=1,
        description="基准策略 + 插槽 diff → L3 组合回测；对照=基准策略，Δ指标",
        validate_spec=_spec_errors,
        subject_key=_subject_key,
        runner=runner or run_portfolio_backtest,
    )


register_evaluation(build_module())
