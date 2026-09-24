"""`head_to_head@1` 评估模块（决策 E5，详设 §6.1.2 形态预留 / §6.1.4 第三种形态）。

策略 A 与策略 B 直接对照（对照 = 另一策略版本，deltas 互算，不走
blank-base / benchmark 阶梯）。适用场景："两条策略线谁优"。

spec: {base: "strategyA@ver", ref: "strategyB@ver", window: [start, end]?}
"""

from __future__ import annotations

import json
import re

import numpy as np

from research.evaluations.base import EvaluationModule, register_evaluation

_VERSION_REF = re.compile(r"^[^@\s]+@[1-9]\d*$")


def _spec_errors(spec: dict, ctx: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    base = str(spec.get("base") or "").strip()
    ref = str(spec.get("ref") or "").strip()
    for field, value in (("base", base), ("ref", ref)):
        if not value:
            errors.append(f"spec.{field} is required (strategy version ref)")
        elif not _VERSION_REF.match(value):
            errors.append(f"spec.{field} must be <id>@<version>, got {value!r}")
    if base and ref and base == ref:
        errors.append("spec.base and spec.ref must differ")
    db = ctx.get("db")
    if db is not None and not errors:
        from portfolio.library import get_version

        for field, value in (("base", base), ("ref", ref)):
            if get_version(db, value) is None:
                errors.append(f"spec.{field} version not in library: {value}")
    return errors


def _subject_key(spec: dict) -> str:
    base = str((spec or {}).get("base") or "")
    ref = str((spec or {}).get("ref") or "")
    return "~".join(sorted([base, ref]))


def run_head_to_head(db, experiment: dict, ctx: dict) -> dict:
    from portfolio import service as portfolio_service
    from portfolio.library import require_version
    from portfolio.reports import daily_returns as _daily_returns
    from portfolio.seed import seed_default_library
    from portfolio.slots import REGISTRY, ensure_builtins
    from portfolio.strategy import parse_strategy_yaml
    from research import holdout
    from research.stats.bootstrap import _circular_blocks
    from research.stats.psr import moments as _moments
    from research.stats.psr import psr as _psr

    registry = (ctx or {}).get("registry") or REGISTRY
    ensure_builtins()
    seed_default_library(db, registry)

    spec = json.loads(experiment["spec_json"]) if isinstance(experiment["spec_json"], str) else experiment["spec_json"]
    a_ref, b_ref = str(spec["base"]), str(spec["ref"])

    windows = holdout.get_windows(db)
    window = spec.get("window") or [windows["sample_start"], windows["sample_end"]]
    start, end = str(window[0]), str(window[1])
    touched = holdout.check_window(
        db, start=start, end=end, experiment_id=experiment["id"],
        token_id=(ctx or {}).get("holdout_token"),
    )
    window_kind = "holdout" if touched else "sample"

    run_params = {
        "window": [start, end], "window_kind": window_kind,
        "initial_capital": float(spec.get("initial_capital", 1_000_000)),
    }
    cfg_a = parse_strategy_yaml(require_version(db, a_ref)["config_yaml"], registry)
    cfg_b = parse_strategy_yaml(require_version(db, b_ref)["config_yaml"], registry)
    result_a = portfolio_service.run_backtest(
        db, config=cfg_a, registry=registry, run_params=run_params, strategy_ref=a_ref,
    )
    result_b = portfolio_service.run_backtest(
        db, config=cfg_b, registry=registry, run_params=run_params, strategy_ref=b_ref,
    )
    runs_out = [
        {"engine_run_id": result_a["run_id"], "window_start": start, "window_end": end,
         "window_kind": window_kind, "holdout_touched": touched},
        {"engine_run_id": result_b["run_id"], "window_start": start, "window_end": end,
         "window_kind": window_kind, "holdout_touched": touched},
    ]

    rets_a = _daily_returns(result_a["daily_nav"]).dropna()
    rets_b = _daily_returns(result_b["daily_nav"]).dropna()
    joined = rets_a.to_frame("a").join(rets_b.to_frame("b"), how="inner").dropna()
    ra, rb = joined["a"].to_numpy(), joined["b"].to_numpy()

    mean_a, std_a, skew_a, kurt_a = _moments(ra)
    mean_b, std_b, _, _ = _moments(rb)
    sr_a = mean_a / std_a if std_a > 0 else 0.0
    sr_b = mean_b / std_b if std_b > 0 else 0.0

    # 配对日收益差：均值 / t 统计 / 区块 bootstrap ΔSharpe 置信带
    diff = ra - rb
    t_stat = float(diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff))) if len(diff) > 1 and diff.std(ddof=1) > 0 else 0.0
    rng = np.random.default_rng(7)
    d_sharpes = []
    n = len(joined)
    if n >= 30:
        block = max(2, int(round(np.sqrt(n))))
        for _ in range(500):
            idx = _circular_blocks(n, block, rng)
            sa, sb = ra[idx], rb[idx]
            std_sa, std_sb = sa.std(ddof=1), sb.std(ddof=1)
            if std_sa > 0 and std_sb > 0:
                d_sharpes.append(float(sa.mean() / std_sa - sb.mean() / std_sb))
    d_band = (
        {"low": float(np.percentile(d_sharpes, 2.5)),
         "high": float(np.percentile(d_sharpes, 97.5))}
        if len(d_sharpes) >= 50 else None
    )
    psr_ab = _psr(sr_a, sr_b, len(ra), skew_a, kurt_a)

    from rule_backtest.metrics import compute_summary
    from research.evaluations._common import long_window_annotations

    # R1-P2-5：换手不得报假 0（DS-P1-3 同类残留）——两条腿都落了 engine_runs，
    # 按 fills 实算成交总额。
    def _real_turnover(result: dict) -> float:
        fills = (
            portfolio_service.load_fills(db, result["run_id"])
            if result.get("run_id") else []
        )
        return sum(float(f["quantity"]) * float(f["fill_price"]) for f in fills)

    summary_a = compute_summary(result_a["daily_nav"], trades=[], turnover_total=_real_turnover(result_a))
    summary_b = compute_summary(result_b["daily_nav"], trades=[], turnover_total=_real_turnover(result_b))

    evidence = {
        "deltas": {
            "delta_annual_return": float(summary_a["annual_return"] - summary_b["annual_return"]),
            "delta_sharpe": float(summary_a["sharpe"] - summary_b["sharpe"]),
            "delta_max_drawdown": float(summary_a["max_drawdown"] - summary_b["max_drawdown"]),
        },
        "paired": {
            "n_days": len(joined),
            "mean_daily_diff": float(diff.mean()) if len(diff) else None,
            "t_stat": t_stat,
            "delta_sharpe_band": d_band,
            "psr_a_over_b": psr_ab,
        },
    }
    warnings = [
        "survivorship_bias(universe 为当前池穿越历史)",
        # R1-P2-5：长窗口三注记（§6.6.4 对全部评估模块生效，此前漏接）
        *long_window_annotations(start),
    ]
    if touched:
        warnings.append("holdout_touched")

    suggested = "inconclusive"
    if d_band is not None and len(joined) >= 30:
        if d_band["low"] > 0 and psr_ab >= 0.95:
            suggested = "confirmed"   # A 显著优于 B
        elif d_band["high"] < 0 and psr_ab <= 0.05:
            suggested = "rejected"    # A 显著劣于 B

    report = {
        "spec": spec, "window": [start, end],
        "engine_runs": [r["engine_run_id"] for r in runs_out],
        "summary_a": {k: summary_a[k] for k in ("annual_return", "max_drawdown", "sharpe", "sortino")},
        "summary_b": {k: summary_b[k] for k in ("annual_return", "max_drawdown", "sharpe", "sortino")},
        "evidence": evidence, "warnings": warnings,
    }
    return {
        "baseline": {"kind": "strategy", "ref": b_ref,
                     "summary": {k: summary_b[k] for k in ("annual_return", "max_drawdown", "sharpe")}},
        "evidence": evidence,
        "warnings": warnings,
        "report": report,
        "suggested_verdict": suggested,
        "runs": runs_out,
    }


def build_module() -> EvaluationModule:
    return EvaluationModule(
        name="head_to_head", version=1,
        description="策略 A vs 策略 B 直接对照（对照=另一策略版本）",
        validate_spec=_spec_errors,
        subject_key=_subject_key,
        runner=run_head_to_head,
    )


register_evaluation(build_module())
