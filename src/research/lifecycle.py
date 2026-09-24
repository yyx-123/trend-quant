"""实验生命周期状态机（详设 §6.4）。

```
proposed ──骨架校验──► rejected_intake
    │ 通过
    ▼
queued ──池调度──► running ──取证完成──► evaluating ──confirm──► verdicted
                      └────── 工程失败 ──────► failed
```

状态转移只能经本模块的 ``transition`` 触发；非法转移直接拒绝（平台强制，
不靠自觉）。三个真正终态：verdicted / failed / rejected_intake；
``archived`` 只是已 verdicted 记录的归档标记（独立字段，不是状态）。
"""

from __future__ import annotations

from research.errors import LifecycleError
from research.ledger import row_to_dict, rows_to_dicts

TERMINAL_STATUSES = frozenset({"verdicted", "failed", "rejected_intake"})

# 合法转移表：(from, to)。proposed → queued / rejected_intake 在创建时落定。
_TRANSITIONS = frozenset(
    {
        ("proposed", "queued"),
        ("proposed", "rejected_intake"),
        ("queued", "running"),
        ("running", "evaluating"),
        ("running", "failed"),
        ("evaluating", "failed"),
        ("evaluating", "verdicted"),
    }
)

# 进入 evaluating → verdicted 的前置在 verdict.confirm_verdict 里校验
# （final_verdict + reasoning 均已落定才允许转移）。


def get_experiment(db, experiment_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
    return row_to_dict(row)


def require_experiment(db, experiment_id: str) -> dict:
    exp = get_experiment(db, experiment_id)
    if exp is None:
        raise LifecycleError(f"experiment not found: {experiment_id}")
    return exp


def transition(
    db,
    experiment_id: str,
    to_status: str,
    *,
    error: str | None = None,
    reject_reason: str | None = None,
) -> dict:
    """执行一次状态转移；非法转移抛 LifecycleError。

    原子认领（loop-review R1-P1-2）：UPDATE 带 ``AND status = ?`` 守卫——
    transition 先读的 from_status 只作转移表校验，真正的占用判定在
    UPDATE 的 WHERE 里。两个通道（app worker 与 CLI --run）同时认领同一
    queued 实验时，只有一个 UPDATE 的 rowcount 为 1，另一个抛
    LifecycleError——"先读后写"竞态从机制上关死。
    """
    exp = require_experiment(db, experiment_id)
    from_status = exp["status"]
    if (from_status, to_status) not in _TRANSITIONS:
        raise LifecycleError(
            f"illegal transition {from_status} -> {to_status} for {experiment_id}"
        )
    fields: dict[str, object] = {"status": to_status}
    if to_status == "running":
        fields["started_at"] = _now()
    if to_status in TERMINAL_STATUSES:
        fields["finished_at"] = _now()
    if error is not None:
        fields["error"] = str(error)[:4000]
    if reject_reason is not None:
        fields["reject_reason"] = str(reject_reason)[:4000]

    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE research_experiments SET {assignments} "
            f"WHERE id = ? AND status = ?",
            (*fields.values(), experiment_id, from_status),
        )
        claimed = int(cur.rowcount or 0) > 0
    if not claimed:
        # 并发通道已抢先完成该转移（或状态已被第三方改写）——按当前
        # 实际状态给出可诊断的失败，而不是静默双写。
        current = get_experiment(db, experiment_id)
        actual = current["status"] if current else "<deleted>"
        raise LifecycleError(
            f"transition {from_status} -> {to_status} lost the race for "
            f"{experiment_id} (current status: {actual})"
        )
    return get_experiment(db, experiment_id)


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def list_experiments(
    db,
    *,
    status: str | None = None,
    topic_id: str | None = None,
    subject_key: str | None = None,
    include_archived: bool = False,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if topic_id:
        clauses.append("topic_id = ?")
        params.append(topic_id)
    if subject_key:
        clauses.append("subject_key = ?")
        params.append(subject_key)
    if not include_archived:
        clauses.append("archived = 0")
    sql = "SELECT * FROM research_experiments"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return rows_to_dicts(rows)


def list_stale_evaluating(db, days: int = 7) -> list[dict]:
    """烂尾巡检（详设 §6.4）：evaluating 超过 N 天未确认的实验。

    平台不自动关闭（结论不能伪造），但烂尾清单对 owner 提醒、台账可见。
    基准时间（loop-review R1-P3-11）：优先取取证引擎 run 的收口时间
    （= 真正进入"等确认"的时刻），无 run 记录时回退 started_at——否则
    长回测（如 6 天）+ 短等待即被误报烂尾。
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM research_experiments
               WHERE status = 'evaluating'
                 AND COALESCE(
                       (SELECT MAX(r.finished_at)
                          FROM engine_runs r
                         WHERE r.run_id IN (
                               SELECT engine_run_id FROM research_runs
                                WHERE experiment_id = research_experiments.id
                                 AND engine_run_id IS NOT NULL)),
                       started_at) IS NOT NULL
                 AND COALESCE(
                       (SELECT MAX(r.finished_at)
                          FROM engine_runs r
                         WHERE r.run_id IN (
                               SELECT engine_run_id FROM research_runs
                                WHERE experiment_id = research_experiments.id
                                 AND engine_run_id IS NOT NULL)),
                       started_at) <= datetime('now', 'localtime', ?)
               ORDER BY started_at""",
            (f"-{int(days)} days",),
        ).fetchall()
    return rows_to_dicts(rows)


def set_archived(db, experiment_id: str, archived: bool = True) -> dict:
    """归档标记（仅对已 verdicted 的记录；供看板折叠）。"""
    exp = require_experiment(db, experiment_id)
    if exp["status"] != "verdicted":
        raise LifecycleError(
            f"only verdicted experiments can be archived (status={exp['status']})"
        )
    with db.connect() as conn:
        conn.execute(
            "UPDATE research_experiments SET archived = ? WHERE id = ?",
            (1 if archived else 0, experiment_id),
        )
    return get_experiment(db, experiment_id)


def mark_interrupted_research_runs(db) -> dict:
    """启动收割（比照存量 mark_interrupted_batch_runs 惯例）：进程死亡遗留的
    running 实验 → failed（进程死亡 = 工程失败）；running 态 engine_runs → failed。

    谓词收窄（DS-复审-R2 新 P1）：evaluating 是**合法长驻态**——平台 verdict
    已生成、等人工 confirm（详设 §6.4，烂尾巡检也只提醒不关闭）。一次普通重启
    不得把"跑完待确认"的实验判死（否则 verdict 永远无法 confirm，结论从台账
    消失）。故：running 一律收割；evaluating 仅在**没有平台 verdict**（pipeline
    在取证途中死亡）时收割；evaluating + 已有 verdict 的原样保留。

    返回 {"experiments": n, "engine_runs": n}。
    """
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE research_experiments
               SET status = 'failed', finished_at = datetime('now','localtime'),
                   error = 'interrupted: process died (startup sweep)'
               WHERE status = 'running'
                  OR (status = 'evaluating' AND id NOT IN (
                        SELECT experiment_id FROM research_verdicts))"""
        )
        n_exp = int(cur.rowcount or 0)
        cur = conn.execute(
            """UPDATE engine_runs
               SET status = 'failed', finished_at = datetime('now','localtime'),
                   error = 'interrupted: process died (startup sweep)'
               WHERE status = 'running'"""
        )
        n_runs = int(cur.rowcount or 0)
    return {"experiments": n_exp, "engine_runs": n_runs}


def mark_holdout_touched(db, experiment_id: str) -> None:
    """触碰 holdout 必留痕（详设 §6.6.4：约束路径外也一样记录）。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE research_experiments SET holdout_touched = 1 WHERE id = ?",
            (experiment_id,),
        )
