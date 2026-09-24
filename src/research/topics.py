"""研究课题（research_topics）：实验的必选归属（详设 §6.4.1）。

课题 = 一个研究问题；实验 = 对该问题的一次检验。课题创建只需
title + question（无额外成本）；concluded 必须写 conclusion，且平台校验
"引用的实验都已到达 verdicted 终态（不许引用半成品）"。
"""

from __future__ import annotations

from research.errors import TopicError
from research.ledger import alloc_id, loads, row_to_dict, rows_to_dicts
from research.sessions import require_session

# 课题结论分级（决策 20，阶段 5 由平台给建议；作者可降不可升）。
CONCLUSION_GRADES = ("supported", "refuted", "mixed", "insufficient-evidence")


def create_topic(db, *, session_id: str, title: str, question: str) -> dict:
    session = require_session(db, session_id)
    title = str(title or "").strip()
    question = str(question or "").strip()
    if not title:
        raise TopicError("topic title must be non-empty")
    if not question:
        raise TopicError("topic question must be non-empty")
    with db.connect() as conn:
        topic_id = alloc_id(conn, "research_topics", "T", width=3)
        conn.execute(
            """INSERT INTO research_topics
               (id, title, question, owner_session, created_by, status)
               VALUES (?, ?, ?, ?, ?, 'open')""",
            (topic_id, title, question, session["session_id"], session["kind"]),
        )
    return get_topic(db, topic_id)


def get_topic(db, topic_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_topics WHERE id = ?", (topic_id,)
        ).fetchone()
    return row_to_dict(row)


def require_open_topic(db, topic_id: str) -> dict:
    topic = get_topic(db, topic_id)
    if topic is None:
        raise TopicError(f"topic not found: {topic_id}")
    if topic["status"] != "open":
        raise TopicError(f"topic already concluded: {topic_id}")
    return topic


def list_topics(db, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM research_topics"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    topics = rows_to_dicts(rows)
    for topic in topics:
        topic["experiment_ids"] = _topic_experiment_ids(db, topic["id"])
        summary = loads(topic.get("conclusion_summary_json"), None)
        topic["conclusion_summary"] = summary
    return topics


def _topic_experiment_ids(db, topic_id: str) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM research_experiments WHERE topic_id = ? ORDER BY id",
            (topic_id,),
        ).fetchall()
    return [r["id"] for r in rows]


def conclude_topic(
    db,
    *,
    topic_id: str,
    conclusion: str,
    experiment_ids: list[str] | None = None,
    grade: str | None = None,
    summary: dict | None = None,
) -> dict:
    """关闭课题。校验（详设 §6.4.1 + 决策 20 §6.4.2）：

    - 课题下所有实验必须到达终态（不许带着在途实验关题）；
    - 明确引用（experiment_ids）的实验必须都已 verdicted（不许引用半成品）；
    - conclusion 必填；
    - 平台量化摘要先行：调用方未给 summary 时平台自动生成（verdict 计数
      含失败、效应量中位数与方向一致率、课题内 FDR、警告聚合）；
      grade 可降不可升（作者给出的分级不得强过平台建议）。
    """
    from research.lifecycle import TERMINAL_STATUSES

    require_open_topic(db, topic_id)
    conclusion = str(conclusion or "").strip()
    if not conclusion:
        raise TopicError("conclusion must be non-empty")
    if grade is not None and grade not in CONCLUSION_GRADES:
        raise TopicError(f"invalid conclusion grade: {grade}")

    from research.conclusion import build_conclusion_summary, check_grade_allowed

    if summary is None:
        summary = build_conclusion_summary(db, topic_id)
    suggested = summary.get("suggested_grade") or "insufficient-evidence"
    if grade is not None and not check_grade_allowed(suggested, grade):
        raise TopicError(
            f"grade {grade} exceeds platform suggestion {suggested} (downgrade only)"
        )
    final_grade = grade if grade is not None else suggested

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM research_experiments WHERE topic_id = ? ORDER BY id",
            (topic_id,),
        ).fetchall()
    by_id = {r["id"]: r["status"] for r in rows}
    non_terminal = [eid for eid, st in by_id.items() if st not in TERMINAL_STATUSES]
    if non_terminal:
        raise TopicError(f"topic has non-terminal experiments: {non_terminal}")
    for eid in experiment_ids or []:
        if eid not in by_id:
            raise TopicError(f"experiment {eid} does not belong to topic {topic_id}")
        if by_id[eid] != "verdicted":
            raise TopicError(
                f"referenced experiment {eid} is not verdicted (status={by_id[eid]})"
            )

    import json

    with db.connect() as conn:
        conn.execute(
            """UPDATE research_topics
               SET status = 'concluded', conclusion = ?, conclusion_grade = ?,
                   conclusion_summary_json = ?, concluded_at = datetime('now','localtime')
               WHERE id = ?""",
            (
                conclusion,
                final_grade,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                topic_id,
            ),
        )
    return get_topic(db, topic_id)
