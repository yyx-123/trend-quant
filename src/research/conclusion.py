"""课题结论的量化纪律（决策 20，详设 §6.4.2）。

课题结论不允许是纯叙事（挑樱桃风险）。平台在课题 concluded 前自动生成
量化摘要，结论必须建立在它之上：
- verdict 计数（含失败，一个不许藏）；
- 效应量汇总：各子实验 Δ主指标的中位数与方向一致率；
- 家族错误校正：课题内全部实验按 BH/FDR 重新校正，报告校正后仍显著的比例；
- 警告聚合：各子实验 warnings 的并集与频次。

**明确不做**：不把多个实验的 p 值合并成一个"总检验"（Fisher 合并等）——
子实验共享数据与基准，独立性假设不成立，合并 p 值是统计学装饰。
"""

from __future__ import annotations

from collections import Counter

from research.ledger import loads
from research.stats.fdr_pbo import bh_fdr

# 结论分级（平台给建议，作者可降不可升）
GRADE_STRENGTH = {"supported": 2, "refuted": 2, "mixed": 1, "insufficient-evidence": 0}


def _effect_size(verdict_row: dict) -> float | None:
    """各模块的 Δ主指标（backtest: ΔSharpe；event: 主 horizon delta_mean；
    bucket: q_spread；distribution: 无效应量）。"""
    evidence = loads(verdict_row.get("evidence_json"), {})
    if "deltas_vs_base" in evidence:
        return evidence["deltas_vs_base"].get("delta_sharpe")
    if "per_horizon" in evidence:
        h = str(evidence.get("primary_horizon", ""))
        return (evidence["per_horizon"].get(h) or {}).get("delta_mean")
    if "q_spread" in evidence:
        return evidence.get("q_spread")
    return None


def build_conclusion_summary(db, topic_id: str) -> dict:
    """平台量化摘要（concluded 前置条件）。"""
    with db.connect() as conn:
        exps = conn.execute(
            """SELECT id, evaluation_module, status, spec_json FROM research_experiments
               WHERE topic_id = ? ORDER BY id""",
            (topic_id,),
        ).fetchall()
        verdicts = conn.execute(
            """SELECT v.* FROM research_verdicts v
               JOIN research_experiments e ON e.id = v.experiment_id
               WHERE e.topic_id = ? AND v.final_verdict IS NOT NULL
               ORDER BY v.experiment_id, v.id""",
            (topic_id,),
        ).fetchall()

    # 每个实验取定论 verdict——定论语义（GLM53F-P1-4）：supersedes IS NULL
    # 的原生/确认链优先于复核稿（复核 verdict 带 supersedes 指针且由平台自动
    # 落定 final=suggested，若按 id 后写覆盖，未经人审的复核稿可翻转课题
    # 分级），与 verdict.latest_verdict 同语义；复核稿只经 supersedes 反查可见
    latest: dict[str, dict] = {}
    for v in verdicts:
        exp_id = v["experiment_id"]
        prev = latest.get(exp_id)
        if prev is None:
            latest[exp_id] = dict(v)
        elif prev["supersedes"] is not None and v["supersedes"] is None:
            latest[exp_id] = dict(v)  # 原生定论覆盖复核稿
        elif (prev["supersedes"] is None) == (v["supersedes"] is None) and v["id"] > prev["id"]:
            latest[exp_id] = dict(v)  # 同类链内取新

    counts = Counter()
    effects: list[float] = []
    effects_by_module: dict[str, list[float]] = {}
    warnings_counter: Counter = Counter()
    pvals: list[float] = []
    dir_hits = 0
    dir_total = 0
    for exp in exps:
        v = latest.get(exp["id"])
        if v is None:
            counts["no_verdict"] += 1
            continue
        counts[v["final_verdict"]] += 1
        effect = _effect_size(v)
        if effect is not None:
            effects.append(effect)
            effects_by_module.setdefault(
                str(exp["evaluation_module"]).split("@")[0], []
            ).append(effect)
            # 方向一致率（GLM53F-P2-2，详设 §6.4.2 明文口径）：与**假设**
            # （spec.expect）同向的占比——不是"与多数方向一致"（那个构造上
            # 恒 ≥0.5，配 0.7 门槛近乎恒真）
            expect = str(loads(exp["spec_json"], {}).get("expect", "positive"))
            # effect == 0 无方向——不计入一致率的分子或分母
            # （旧行为 (0>0)==False 在 expect=negative 时把零效应计成命中）
            expect_positive = expect != "negative"
            if effect != 0:
                dir_total += 1
                if (effect > 0) == expect_positive:
                    dir_hits += 1
        for w in loads(v.get("warnings_json"), []):
            warnings_counter[w.split("(")[0]] += 1
        evidence_all = loads(v.get("evidence_json"), {})
        stats = evidence_all.get("stats") or {}
        p_val = None
        # 退化腿（零成交/全现金）的 p 值是浮点噪声，
        # **不进课题 FDR 家族**（否则"零成交"实验会被算成显著）
        if evidence_all.get("degenerate_legs"):
            p_val = None
        elif stats.get("psr") is not None:
            p_val = max(0.0, min(1.0, 1.0 - float(stats["psr"])))
        elif evidence_all.get("p_value") is not None:
            p_val = max(0.0, min(1.0, float(evidence_all["p_value"])))
        if p_val is not None:
            pvals.append(p_val)

    direction_consistency = (dir_hits / dir_total) if dir_total else None

    fdr = bh_fdr(pvals, q=0.05) if pvals else []
    still_significant = sum(1 for f in fdr if f["significant"])

    # 平台分级建议
    confirmed, rejected = counts.get("confirmed", 0), counts.get("rejected", 0)
    # 无效应量可算的实验（如 distribution）不参与方向一致率卡控
    dc_ok = direction_consistency is None or direction_consistency >= 0.7
    if not latest:
        suggested = "insufficient-evidence"
    elif confirmed > 0 and rejected == 0 and dc_ok:
        suggested = "supported"
    elif rejected > confirmed:
        suggested = "refuted"
    elif confirmed > 0 and rejected > 0:
        suggested = "mixed"
    else:
        suggested = "insufficient-evidence"

    import numpy as np

    return {
        "verdict_counts": dict(counts),
        "effect_sizes": {
            "n": len(effects),
            "median": float(np.median(effects)) if effects else None,
            "direction_consistency": direction_consistency,
            "by_module": {
                k: {"n": len(v), "median": float(np.median(v))}
                for k, v in effects_by_module.items()
            },
            "note": "方向一致率 = 与假设（spec.expect）同向的占比（§6.4.2）；"
                    "median 为跨族混合口径，分族中位数（by_module）才可解释",
        },
        "fdr": {"n_tested": len(pvals), "still_significant": still_significant},
        "warnings_aggregated": dict(warnings_counter),
        "suggested_grade": suggested,
        "note": "不做跨实验 p 值合并（独立性不成立）；分级建议可降不可升",
    }


def check_grade_allowed(suggested: str, author_grade: str) -> bool:
    """平台建议可降不可升 + 同向约束（评审 K3-P2-7：supported↔refuted 平级
    翻转不是降级——方向相反的结论不许顶替）。"""
    if suggested not in GRADE_STRENGTH or author_grade not in GRADE_STRENGTH:
        return False
    if {suggested, author_grade} == {"supported", "refuted"}:
        return False
    return GRADE_STRENGTH[author_grade] <= GRADE_STRENGTH[suggested]
