"""`distribution@1` 评估模块（详设 §6.5.4）。

回答的问题形态：这个参数该怎么标定。
取证方式：全市场池化分布统计（均值/σ/偏度/分位数，按类型×年度分组）。
判定：不对"有效性"下结论，verdict 固定 inconclusive 一档 + 标定建议；
**不能**作为策略改动的直接依据（要改动策略，仍需 backtest 实验）。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from research.evaluations._common import (
    load_eval_panel,
    long_window_annotations,
    resolve_universe_symbols,
)
from research.evaluations.base import EvaluationModule, register_evaluation
from research.evaluations.bucket import _feature_matrix

METRICS = ("atr_pct", "momentum_20", "er_10")


def _spec_errors(spec: dict, ctx: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    if str(spec.get("metric") or "") not in METRICS:
        errors.append(f"spec.metric must be one of {METRICS}")
    return errors


def _subject_key(spec: dict) -> str:
    return str((spec or {}).get("metric") or "")


def run_distribution(db, experiment: dict, ctx: dict) -> dict:
    from research import holdout

    spec = json.loads(experiment["spec_json"]) if isinstance(experiment["spec_json"], str) else experiment["spec_json"]
    metric = str(spec["metric"])

    windows = holdout.get_windows(db)
    window = spec.get("window") or [windows["sample_start"], windows["sample_end"]]
    start, end = str(window[0]), str(window[1])
    touched = holdout.check_window(
        db, start=start, end=end, experiment_id=experiment["id"],
        token_id=(ctx or {}).get("holdout_token"),
    )

    symbols = resolve_universe_symbols(db, spec.get("universe"))
    panel = load_eval_panel(
        db, symbols=symbols, start=start, end=end, experiment_id=experiment["id"],
    )
    matrix = _feature_matrix(panel, metric)
    start_day, end_day = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    in_window = np.array([start_day <= d <= end_day for d in panel.dates])
    values = matrix[in_window, :]
    values = values[np.isfinite(values)]

    quantiles = {}
    if len(values):
        for q in (1, 5, 25, 50, 75, 95, 99):
            quantiles[f"p{q}"] = float(np.percentile(values, q))

    # 按年度分组
    by_year: dict[str, dict] = {}
    for year in sorted({d.year for d in panel.dates if start_day <= d <= end_day}):
        mask = np.array([d.year == year and start_day <= d <= end_day for d in panel.dates])
        vals = matrix[mask, :]
        vals = vals[np.isfinite(vals)]
        if len(vals):
            by_year[str(year)] = {
                "n": len(vals),
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0,
                "p50": float(np.percentile(vals, 50)),
            }

    evidence = {
        "metric": metric,
        "n": len(values),
        "mean": float(np.mean(values)) if len(values) else None,
        "std": float(np.std(values, ddof=1)) if len(values) > 1 else None,
        "skew": float(pd.Series(values).skew()) if len(values) > 2 else None,
        "quantiles": quantiles,
        "by_year": by_year,
    }
    # 标定建议：调用方在 spec.criterion 里声明判断准则（文本），平台不臆造
    calibration_note = spec.get("criterion") or "（未声明判断准则，供后续实验引用）"

    warnings = ["survivorship_bias(universe 为当前池穿越历史)"]
    # 长窗口三注记按窗口生效（DS-复审-R2 §4-1：不只 backtest 路径）
    warnings.extend(long_window_annotations(start))

    report = {
        "spec": spec, "window": [start, end], "symbols": len(panel.symbols),
        "evidence": evidence, "warnings": warnings,
        "calibration_note": calibration_note,
    }
    return {
        "baseline": {"kind": "preset_criterion", "criterion": calibration_note},
        "evidence": evidence,
        "warnings": warnings,
        "report": report,
        "suggested_verdict": "inconclusive",  # 固定档（详设 §6.5.4）
        "runs": [{
            "engine_run_id": None, "window_start": start, "window_end": end,
            "window_kind": "holdout" if touched else "sample", "holdout_touched": touched,
        }],
    }


def build_module() -> EvaluationModule:
    return EvaluationModule(
        name="distribution", version=1,
        description="池化分布统计（参数标定建议；verdict 固定 inconclusive）",
        validate_spec=_spec_errors,
        subject_key=_subject_key,
        runner=run_distribution,
        allowed_finals=("inconclusive",),
    )


register_evaluation(build_module())
