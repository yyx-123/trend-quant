"""verdict 统一信封与确认流（详设 §6.5/§6.4）。

- 平台生成：baseline / evidence / warnings / report / suggested_verdict
  全部由平台流水线写入（append-only；AI 只能写 reasoning）；
- 确认：final_verdict 必填、reasoning 必填，可降不可升（VERDICT_RANK），
  且受评估模块 allowed_finals 限制（distribution 固定 inconclusive）；
- 确认落定后实验才允许 evaluating → verdicted。
"""

from __future__ import annotations

from research import evaluations
from research.errors import LifecycleError
from research.ledger import alloc_id, loads, row_to_dict, rows_to_dicts
from research.lifecycle import require_experiment, transition

_VERDICT_COLS = (
    "baseline_json",
    "evidence_json",
    "warnings_json",
    "report_json",
)


def insert_platform_verdict(
    db,
    *,
    experiment_id: str,
    baseline: dict,
    evidence: dict,
    warnings: list[str],
    report: dict,
    suggested_verdict: str,
    holdout_touched: bool = False,
    attempt_index_snapshot: int = 1,
    supersedes: str | None = None,
) -> dict:
    """平台流水线写入一条 verdict（内容随后即不可改）。"""
    if suggested_verdict not in ("confirmed", "rejected", "inconclusive"):
        raise LifecycleError(f"invalid suggested_verdict: {suggested_verdict}")
    require_experiment(db, experiment_id)
    import json

    with db.connect() as conn:
        vid = alloc_id(conn, "research_verdicts", "V", width=5)
        conn.execute(
            """INSERT INTO research_verdicts
               (id, experiment_id, baseline_json, evidence_json, warnings_json,
                report_json, suggested_verdict, holdout_touched,
                attempt_index_snapshot, supersedes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vid,
                experiment_id,
                json.dumps(baseline or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(warnings or [], ensure_ascii=False),
                json.dumps(report or {}, ensure_ascii=False, sort_keys=True),
                suggested_verdict,
                1 if holdout_touched else 0,
                int(attempt_index_snapshot),
                supersedes,
            ),
        )
    return get_verdict(db, vid)


def get_verdict(db, verdict_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_verdicts WHERE id = ?", (verdict_id,)
        ).fetchone()
    return _decode(row_to_dict(row))


def list_verdicts(db, experiment_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM research_verdicts WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        ).fetchall()
    return [_decode(r) for r in rows_to_dicts(rows)]


def latest_verdict(db, experiment_id: str) -> dict | None:
    """最新 verdict。定论语义（评审 K3-P2-6/DS-P2-3）：supersedes IS NULL 的
    行（原生/确认链）优先于复核稿——复核 verdict 带 supersedes 指针，不劫持
    台账定论展示位；"谁被取代"由反向查询得出（§6.6.7）。"""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT * FROM research_verdicts
               WHERE experiment_id = ?
               ORDER BY (supersedes IS NULL) DESC, id DESC LIMIT 1""",
            (experiment_id,),
        ).fetchone()
    return _decode(row_to_dict(row))


def canonical_verdict(verdicts: list[dict]) -> dict | None:
    """从（按 id 升序的）verdict 列表中取定论行——与 latest_verdict 同语义
    （GLM53F-P1-4：supersedes IS NULL 优先，同类链内取最新）。展示面/摘要
    统一走本选择器，禁止直接 verdicts[-1]（复核稿 id 更大，会劫持定论位）。"""
    if not verdicts:
        return None
    canonical = [v for v in verdicts if v.get("supersedes") is None]
    return canonical[-1] if canonical else verdicts[-1]


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    row["baseline"] = loads(row.get("baseline_json"), {})
    row["evidence"] = loads(row.get("evidence_json"), {})
    row["warnings"] = loads(row.get("warnings_json"), [])
    row["report"] = loads(row.get("report_json"), {})
    return row


def confirm_verdict(
    db,
    *,
    experiment_id: str,
    final_verdict: str,
    reasoning: str,
    session_id: str,
) -> dict:
    """落定 final_verdict + reasoning 并把实验推进到 verdicted。

    规则（详设 §6.4）：final 允许降级、不允许升过平台建议；reasoning 必填；
    前置是实验处于 evaluating（平台 verdict 已生成）。
    """
    from research.sessions import require_session

    session = require_session(db, session_id)
    exp = require_experiment(db, experiment_id)
    if exp["status"] != "evaluating":
        raise LifecycleError(
            f"experiment must be evaluating to confirm verdict (status={exp['status']})"
        )
    verdict = latest_verdict(db, experiment_id)
    if verdict is None:
        raise LifecycleError(f"no platform verdict generated for {experiment_id}")
    if verdict["final_verdict"] is not None:
        raise LifecycleError(f"verdict already confirmed for {experiment_id}")

    reasoning = str(reasoning or "").strip()
    if not reasoning:
        raise LifecycleError("reasoning must be non-empty")

    module = evaluations.require_evaluation(exp["evaluation_module"])
    if not module.final_verdict_allowed(verdict["suggested_verdict"], final_verdict):
        raise LifecycleError(
            f"final_verdict {final_verdict} exceeds platform suggestion "
            f"{verdict['suggested_verdict']} (downgrade only)"
        )

    with db.connect() as conn:
        conn.execute(
            """UPDATE research_verdicts
               SET final_verdict = ?, reasoning = ?, confirmed_by = ?,
                   confirmed_at = datetime('now','localtime')
               WHERE id = ?""",
            (final_verdict, reasoning, session["session_id"], verdict["id"]),
        )
    transition(db, experiment_id, "verdicted")
    return latest_verdict(db, experiment_id)
