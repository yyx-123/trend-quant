"""模块升级后的批量复核（决策 E6，详设 §6.6.7）。

模块升级（如 chandelier@1 → @2）后，平台可发起**重跑运动**：批量重跑引用
旧模块版本的历史实验，产出新 verdict。台账 append-only 不破：旧 verdict
一行不改；**指针只写在新记录上**——新复核 verdict 的 ``supersedes`` 指向
被复核的旧 verdict（experiment_id）；"谁已被取代"由台账反向查询得出。
"""

from __future__ import annotations

import json

from audit.app_logger import get_logger
from research import evaluations, lifecycle, verdict
from research.ledger import loads

logger = get_logger(__name__)


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


def _collect_module_refs(obj, refs: set) -> None:
    """递归收集 spec 中的模块引用串（精确判定，loop-review R1-P2-4）。

    两种合法形态：完整引用 "name@version"（dict 的 module/to/from 值）与
    带 `(` 的内联参数形（如 "heat_cap@1(0.06)"）——按 name@ 前缀匹配。"""
    if isinstance(obj, str):
        if "(" in obj:
            head = obj.split("(", 1)[0]
        else:
            head = obj
        refs.add(head.strip())
        return
    if isinstance(obj, list):
        for x in obj:
            _collect_module_refs(x, refs)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("module", "to", "from", "event", "signal_module", "base", "ref"):
                _collect_module_refs(v, refs)
            else:
                _collect_module_refs(v, refs)


def find_experiments_using(db, module_ref: str) -> list[dict]:
    """spec 中**精确引用**某模块版本的全部实验（verdicted 才复核——半成品
    不复核）。

    R1-P2-4：SQL LIKE 只作初筛；命中与否由解析后的引用集**精确相等**判定。
    此前 `module_ref in text` 的子串判断会让 `stop@1` 的 campaign 误伤所有
    引用 `hard_stop@1`/`ma_stop@1` 的实验——被误伤实验按原 spec 重跑并写入
    带错误 recompute_with 标签的复核 verdict，污染台账。
    """
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
        refs: set = set()
        _collect_module_refs(spec, refs)
        if module_ref in refs:
            out.append({"id": row["id"], "spec": spec})
    return out


def holdout_blocks_campaign(db, spec: dict) -> bool:
    """该 spec 的窗口是否触碰 holdout（campaign 无 token 通路 → 必然失败的剔除判据）。

    R1-P3-18：显式记账为 skipped，而不是让 HoldoutError 混进 failed 的原因字符串
    ——批量复核"静默丢目标"必须可见。判定失败（异常）不阻断复核（交给 runner 自行拒绝）。
    """
    try:
        from research import holdout as _holdout

        win = (spec or {}).get("window") or []
        if len(win) == 2:
            return bool(_holdout.window_touches_holdout(db, win[0], win[1]))
    except Exception:
        return False
    return False


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
        # R1-P3-18：campaign 没有 holdout token 的传递通路（CLI/MCP 都没有
        # 参数），触碰过 holdout 的目标实验必然以 HoldoutError 报 failed——
        # 批量复核会"静默丢目标"。这里先判定，命中即按 skipped 显式记账，
        # 让"有哪些目标没复核"在结果里可见（而不是混进 failed 的原因字符串）。
        if holdout_blocks_campaign(db, new_spec):
            skipped.append({
                "id": exp_id,
                "reason": "holdout_touched（campaign 无 token 传递通路，"
                          "需人工单跑并附带 holdout token）",
            })
            continue
        # 复核运行：直接调 runner（不走状态机——原实验已是 verdicted 终态，
        # 复核是台账追加，不是生命周期重开）
        try:
            parts = module.runner(
                db, {**exp, "spec_json": json.dumps(new_spec, ensure_ascii=False)},
                {"registry": registry, "holdout_token": None},
            )
        except Exception as exc:  # 复核失败不污染原记录
            failed.append({"id": exp_id, "topic_id": exp.get("topic_id"),
                           "error": str(exc)[:500]})
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
        # R1-P3-10：复核 run 同样落 research_runs（experiment→runs→engine_runs
        # 血缘链在复核路径不得断——否则复核 verdict 查不到取证运行）
        from research import runs as _runs

        for r in parts.get("runs", []):
            try:
                _runs.insert_run(
                    db, experiment_id=exp_id,
                    engine_run_id=r.get("engine_run_id"),
                    window_start=r.get("window_start"), window_end=r.get("window_end"),
                    window_kind=r.get("window_kind", "sample"),
                    holdout_touched=bool(r.get("holdout_touched")),
                )
            except Exception:
                # 血缘补录失败不影响复核结论（runs 行非判定输入），但必须
                # 可见——静默吞掉会连 window_kind 非法这类真实错误一起藏
                logger.warning(
                    "recompute: research_runs backfill failed for %s", exp_id,
                    exc_info=True,
                )
        recomputed.append({"id": exp_id, "verdict_id": v["id"],
                           "topic_id": exp.get("topic_id")})
    # R3C-P3-5（Round 3 复核）：复核给实验追加了带 supersedes 的 verdict 之后
    # 必须**重新物化受影响课题**——物化是 DB→文件的单向生成，此前只有
    # confirm/conclude 会触发，复核产物长期不进审计文件夹（DB 与文件夹不一致）。
    # 失败只告警，与 service._materialize 同口径。
    rematerialized: list[str] = []
    try:
        from research.topic_files import materialize_topic

        topic_ids = {
            row["topic_id"] for row in recomputed if row.get("topic_id")
        } | {row["topic_id"] for row in failed if row.get("topic_id")}
        for topic_id in sorted(topic_ids):
            if not topic_id:
                continue
            try:
                materialize_topic(db, topic_id)
                rematerialized.append(topic_id)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "recompute: re-materialize topic %s failed", topic_id
                )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("recompute: re-materialization skipped")

    return {
        "old": old_module_ref, "new": new_module_ref,
        "recomputed": recomputed, "failed": failed, "skipped": skipped,
        "rematerialized_topics": rematerialized,
    }
