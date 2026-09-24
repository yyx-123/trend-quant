"""模块升级后的批量复核（决策 E6，详设 §6.6.7）。

模块升级（如 chandelier@1 → @2）后，平台可发起**重跑运动**：批量重跑引用
旧模块版本的历史实验，产出新 verdict。台账 append-only 不破：旧 verdict
一行不改；**指针只写在新记录上**——新复核 verdict 的 ``supersedes`` 指向
被复核的旧 verdict（experiment_id）；"谁已被取代"由台账反向查询得出。
"""

from __future__ import annotations

import json

from research import evaluations, lifecycle, verdict
from research.ledger import loads


def _rewrite_module_ref(obj, old_ref: str, new_ref: str):
    """递归替换 spec 中的模块引用（diff[].to / event / signal_module 字符串）。"""
    if isinstance(obj, str):
        if obj == old_ref:
            return new_ref
        if obj.startswith(f"{old_ref}("):
            return f"{new_ref}(" + obj.split("(", 1)[1]
        return obj
    if isinstance(obj, list):
        return [_rewrite_module_ref(x, old_ref, new_ref) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "module" and v == old_ref:
                out[k] = new_ref
            else:
                out[k] = _rewrite_module_ref(v, old_ref, new_ref)
        return out
    return obj


def find_experiments_using(db, module_ref: str) -> list[dict]:
    """spec 中引用某模块版本的全部实验（verdicted 才复核——半成品不复核）。"""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, spec_json FROM research_experiments
               WHERE status = 'verdicted' AND spec_json LIKE ?
               ORDER BY id""",
            (f"%{module_ref}%",),
        ).fetchall()
    out = []
    for row in rows:
        spec = loads(row["spec_json"], {})
        text = json.dumps(spec, ensure_ascii=False)
        if module_ref in text:
            out.append({"id": row["id"], "spec": spec})
    return out


def recompute_campaign(
    db,
    *,
    old_module_ref: str,
    new_module_ref: str,
    registry,
    session_id: str = "human-default",
    limit: int | None = None,
) -> dict:
    """批量复核：重跑引用旧模块版本的 verdicted 实验，产出带 supersedes 指针
    的新 verdict。返回 {recomputed: [...], failed: [...], skipped: [...]}。

    实现方式：为每个历史实验构造一个 spec 已替换的复核运行（不改变原实验
    的状态/血缘——复核 verdict 挂在原实验上，supersedes 指向它）。
    """
    from research.sessions import require_session

    require_session(db, session_id)
    targets = find_experiments_using(db, old_module_ref)
    if limit:
        targets = targets[: int(limit)]
    recomputed, failed, skipped = [], [], []
    for item in targets:
        exp_id = item["id"]
        exp = lifecycle.get_experiment(db, exp_id)
        module = evaluations.get_evaluation(exp["evaluation_module"])
        if module is None or module.runner is None:
            skipped.append({"id": exp_id, "reason": "evaluation module not runnable"})
            continue
        new_spec = _rewrite_module_ref(item["spec"], old_ref=old_module_ref, new_ref=new_module_ref)
        # 复核运行：直接调 runner（不走状态机——原实验已是 verdicted 终态，
        # 复核是台账追加，不是生命周期重开）
        try:
            parts = module.runner(
                db, {**exp, "spec_json": json.dumps(new_spec, ensure_ascii=False)},
                {"registry": registry, "holdout_token": None},
            )
        except Exception as exc:  # 复核失败不污染原记录
            failed.append({"id": exp_id, "error": str(exc)[:500]})
            continue
        # 复核产物是平台终态记录（不进"待确认"流）：final = suggested 自动落定
        # 并带标记；定论展示面仍属原 verdict（latest_verdict 优先 supersedes IS NULL）
        v = verdict.insert_platform_verdict(
            db,
            experiment_id=exp_id,
            baseline=parts.get("baseline", {}),
            evidence={**parts.get("evidence", {}), "recompute_with": new_module_ref},
            warnings=[*parts.get("warnings", []), f"recompute_campaign({old_module_ref}→{new_module_ref})"],
            report=parts.get("report", {}),
            suggested_verdict=parts.get("suggested_verdict", "inconclusive"),
            holdout_touched=any(r.get("holdout_touched") for r in parts.get("runs", [])),
            attempt_index_snapshot=int(exp["attempt_index"]),
            supersedes=exp_id,
        )
        with db.connect() as conn:
            conn.execute(
                """UPDATE research_verdicts
                   SET final_verdict = suggested_verdict,
                       reasoning = 'platform recompute campaign（平台复核产物自动落定）',
                       confirmed_by = 'platform', confirmed_at = datetime('now','localtime')
                   WHERE id = ?""",
                (v["id"],),
            )
        recomputed.append({"id": exp_id, "verdict_id": v["id"]})
    return {
        "old": old_module_ref, "new": new_module_ref,
        "recomputed": recomputed, "failed": failed, "skipped": skipped,
    }
