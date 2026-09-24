"""实验执行流水线（worker 核心，同步路径；阶段 5 的并发池复用本函数）。

链路（详设 §6.1.4）：
```
propose_experiment → 骨架校验 → queued
  → running（池调度）→ 评估模块取证 → evaluating
  → verdict 平台生成 → confirm_verdict → verdicted
```
工程失败 → failed（入表；不等于假设被证伪 rejected）。
"""

from __future__ import annotations

from audit.app_logger import get_logger
from research import evaluations, lifecycle, runs, verdict
from research.errors import LifecycleError

_logger = get_logger(__name__)


def run_experiment(db, experiment_id: str, *, registry, holdout_token: str | None = None) -> dict:
    """执行一个已 queued 的实验到 evaluating（平台 verdict 已生成）。

    返回最新 verdict 行；工程失败时实验转 failed 并返回实验行。
    """
    exp = lifecycle.require_experiment(db, experiment_id)
    if exp["status"] != "queued":
        raise LifecycleError(f"experiment {experiment_id} not queued (status={exp['status']})")
    module = evaluations.require_evaluation(exp["evaluation_module"])
    if module.runner is None:
        raise LifecycleError(f"evaluation module {module.key} has no runner")

    # holdout token 端到端接通（评审 K3-P1-2/DS-P1-2）：调用方未显式传 token 时，
    # 自动带出**绑定该实验**的未消费 token（发放→运行→消费→留痕闭环）；
    # 全局 token 不自动带出（须显式透传——DS-复审-R2 §4-5）
    if holdout_token is None:
        holdout_token = _pick_unconsumed_token(db, experiment_id)

    lifecycle.transition(db, experiment_id, "running")
    try:
        parts = module.runner(db, exp, {"registry": registry, "holdout_token": holdout_token})
        lifecycle.transition(db, experiment_id, "evaluating")
        touched = any(r.get("holdout_touched") for r in parts.get("runs", []))
        if touched:
            lifecycle.mark_holdout_touched(db, experiment_id)
        for r in parts.get("runs", []):
            runs.insert_run(
                db, experiment_id=experiment_id,
                engine_run_id=r.get("engine_run_id"),
                window_start=r.get("window_start"), window_end=r.get("window_end"),
                window_kind=r.get("window_kind", "sample"),
                holdout_touched=bool(r.get("holdout_touched")),
            )
        return verdict.insert_platform_verdict(
            db,
            experiment_id=experiment_id,
            baseline=parts.get("baseline", {}),
            evidence=parts.get("evidence", {}),
            warnings=parts.get("warnings", []),
            report=parts.get("report", {}),
            suggested_verdict=parts.get("suggested_verdict", "inconclusive"),
            holdout_touched=touched,
            attempt_index_snapshot=exp["attempt_index"],
        )
    except Exception as exc:
        _logger.exception("experiment %s failed", experiment_id)
        lifecycle.transition(db, experiment_id, "failed", error=str(exc))
        return lifecycle.get_experiment(db, experiment_id)


def _pick_unconsumed_token(db, experiment_id: str) -> str | None:
    """只自动带出**绑定本实验**的最早未消费 holdout token。

    K3-R2-注记-2 / DS-复审-R2 §4-5 / GLM53F-P2-5：全局 token
    （experiment_id IS NULL）不自动带出——它会被下一个触碰 holdout 的
    无关实验按 id 抢先消费，与发放意图可能不符；全局 token 须调用方
    显式透传（CLI --token / run_experiment(holdout_token=...)）。
    """
    with db.connect() as conn:
        row = conn.execute(
            """SELECT id FROM holdout_tokens
               WHERE consumed_at IS NULL AND experiment_id = ?
               ORDER BY id LIMIT 1""",
            (experiment_id,),
        ).fetchone()
    return row["id"] if row else None
