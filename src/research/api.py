"""通道无关的服务面（详设 §6.7）：MCP/CLI/Web 皆为薄通道，服务层唯一。

AI 沙箱边界 = 这张清单：组合已审核模块提实验 + 读台账 + 保存模块草稿；
不能让未过测试门的代码进运行路径、不能改判定规则、不能碰 holdout
（grant_holdout 仅 human session）、不能删改记录。
"""

from __future__ import annotations

from pathlib import Path

from research import (
    experiments,
    holdout,
    lifecycle,
    modules,
    sessions,
    topics,
    verdict,
)
from research.errors import ResearchError
from research.ledger import loads

# 课题文件夹默认锚定项目根（评审 DS-P3-11：CWD 相对会把产物写错位置）
DEFAULT_TOPICS_DIR = Path(__file__).resolve().parents[2] / "research" / "topics"


class ResearchService:
    def __init__(self, db, *, registry, worker=None, topics_dir: str | Path | None = None) -> None:
        self.db = db
        self.registry = registry
        self.worker = worker  # ResearchWorker 或 None（同步模式）
        self.topics_dir = Path(topics_dir or DEFAULT_TOPICS_DIR)

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def register_session(self, kind: str, label: str = "", channel: str = "api") -> dict:
        return sessions.register_session(self.db, kind, label, channel)

    def default_human_session(self) -> dict:
        return sessions.get_or_create_default_human_session(self.db)

    # ------------------------------------------------------------------
    # 课题
    # ------------------------------------------------------------------
    def propose_topic(self, *, session_id: str, title: str, question: str) -> dict:
        return topics.create_topic(self.db, session_id=session_id, title=title, question=question)

    def get_topic(self, topic_id: str) -> dict | None:
        topic = topics.get_topic(self.db, topic_id)
        if topic is not None:
            topic["experiment_ids"] = topics._topic_experiment_ids(self.db, topic_id)
        return topic

    def list_topics(self, status: str | None = None) -> list[dict]:
        return topics.list_topics(self.db, status=status)

    def conclude_topic(
        self, *, topic_id: str, conclusion: str, session_id: str,
        experiment_ids: list[str] | None = None, grade: str | None = None,
    ) -> dict:
        result = topics.conclude_topic(
            self.db, topic_id=topic_id, conclusion=conclusion,
            experiment_ids=experiment_ids, grade=grade,
        )
        self._materialize(topic_id)
        return result

    # ------------------------------------------------------------------
    # 实验
    # ------------------------------------------------------------------
    def propose_experiment(
        self, *, session_id: str, title: str, topic_id: str, evaluation_module: str,
        spec: dict, hypothesis: str, parent_experiment_id: str | None = None,
        allow_duplicate: bool = False, auto_queue: bool = True,
    ) -> dict:
        exp = experiments.propose_experiment(
            self.db, session_id=session_id, title=title, topic_id=topic_id,
            evaluation_module=evaluation_module, spec=spec, hypothesis=hypothesis,
            parent_experiment_id=parent_experiment_id, registry=self.registry,
            allow_duplicate=allow_duplicate,
        )
        if auto_queue and self.worker is not None:
            self.worker.submit(exp["id"])
        return exp

    def get_experiment(self, experiment_id: str) -> dict | None:
        return experiments.get_experiment_detail(self.db, experiment_id)

    def list_experiments(self, **filters) -> list[dict]:
        return lifecycle.list_experiments(self.db, **filters)

    def run_status(self, experiment_id: str) -> dict:
        exp = lifecycle.require_experiment(self.db, experiment_id)
        latest = verdict.latest_verdict(self.db, experiment_id)
        return {
            "id": exp["id"], "status": exp["status"],
            "started_at": exp.get("started_at"), "finished_at": exp.get("finished_at"),
            "verdict": None if latest is None else {
                "suggested": latest["suggested_verdict"], "final": latest["final_verdict"],
            },
        }

    def get_run_status(self, experiment_id: str) -> dict:
        """运行状态（§6.7 服务面条目；run_status 的别名）。"""
        return self.run_status(experiment_id)

    def get_run_report(self, experiment_id: str) -> dict | None:
        """实验报告（verdict.report 完整明细 + 引擎 run 血缘；§6.5.0 完整报告）。"""
        detail = self.get_experiment(experiment_id)
        if detail is None:
            return None
        verdicts = detail.get("verdicts", [])
        # 定论优先（GLM53F-P1-4）：复核稿不劫持报告展示位
        latest = verdict.canonical_verdict(verdicts)
        from research import runs as runs_mod

        return {
            "experiment": detail,
            "report": None if latest is None else latest.get("report"),
            "runs": runs_mod.list_runs(self.db, experiment_id),
        }

    def append_experiment_to_topic(self, *, experiment_id: str, topic_id: str,
                                   session_id: str) -> dict:
        """把已创建实验改挂到另一个课题（归属修正）。

        仅允许实验尚未开跑（status=queued）时移动——开跑后的血缘冻结。
        （详设 §6.7 服务面条目；append-only 触发器已将 topic_id 放行，
        归属不是证据内容。）
        """
        exp = lifecycle.require_experiment(self.db, experiment_id)
        if exp["status"] != "queued":
            raise ResearchError(
                f"only queued experiments can move topics (status={exp['status']})"
            )
        topics.require_open_topic(self.db, topic_id)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE research_experiments SET topic_id = ? WHERE id = ?",
                (topic_id, experiment_id),
            )
        return lifecycle.get_experiment(self.db, experiment_id)

    def rerun_experiment(self, *, experiment_id: str, session_id: str,
                         auto_queue: bool = True) -> dict:
        """复现（详设 §6.4.1）：同 spec 同 attempt_index、不计尝试计数。"""
        exp = experiments.rerun_experiment(
            self.db, experiment_id=experiment_id, session_id=session_id
        )
        if auto_queue and self.worker is not None:
            self.worker.submit(exp["id"])
        return exp

    def confirm_verdict(
        self, *, experiment_id: str, final_verdict: str, reasoning: str, session_id: str,
    ) -> dict:
        result = verdict.confirm_verdict(
            self.db, experiment_id=experiment_id, final_verdict=final_verdict,
            reasoning=reasoning, session_id=session_id,
        )
        exp = lifecycle.get_experiment(self.db, experiment_id)
        if exp is not None:
            self._materialize(exp["topic_id"])
        return result

    # ------------------------------------------------------------------
    # 目录与台账查询
    # ------------------------------------------------------------------
    def list_modules(self, slot: str | None = None) -> list[dict]:
        return [
            {
                "key": s.key, "slot": s.slot, "kind": s.kind,
                "description": s.description, "params_schema": s.params_schema,
            }
            for s in self.registry.list(slot=slot)
        ]

    def list_evaluations(self) -> list[dict]:
        from research import evaluations

        return evaluations.list_evaluations()

    def get_strategy_version(self, version_id: str) -> dict | None:
        from portfolio import library

        return library.get_version(self.db, version_id)

    def search_ledger(
        self, *, subject_key: str | None = None, topic_id: str | None = None,
        final_verdict: str | None = None, include_archived: bool = False,
    ) -> list[dict]:
        """台账检索（全员只读开放：含他人失败实验）。"""
        exps = lifecycle.list_experiments(
            self.db, topic_id=topic_id, subject_key=subject_key,
            include_archived=include_archived,
        )
        out = []
        for exp in exps:
            latest = verdict.latest_verdict(self.db, exp["id"])
            if final_verdict and (latest is None or latest["final_verdict"] != final_verdict):
                continue
            out.append({
                "id": exp["id"], "title": exp["title"], "topic_id": exp["topic_id"],
                "evaluation_module": exp["evaluation_module"],
                "subject_key": exp["subject_key"], "status": exp["status"],
                "attempt_index": exp["attempt_index"],
                "final_verdict": None if latest is None else latest["final_verdict"],
                "suggested_verdict": None if latest is None else latest["suggested_verdict"],
                "warnings": [] if latest is None else latest["warnings"],
                "created_at": exp["created_at"],
            })
        return out

    def strategy_line(self, subject_key: str) -> dict:
        """一条研究线的全部实验（含失败）与显著性折扣上下文。

        attempt_count 排除复现 run（GLM53F-P2-6：is_reproduction 不计入
        后续尝试计数，与 experiments.py 的 attempt_index 口径一致——否则
        rerun 多次后显著性折扣上下文虚高）。
        """
        exps = lifecycle.list_experiments(self.db, subject_key=subject_key, include_archived=True)
        return {
            "subject_key": subject_key,
            "attempt_count": len([
                e for e in exps
                if e["status"] != "rejected_intake" and not e.get("is_reproduction")
            ]),
            "experiments": [e["id"] for e in exps],
        }

    def promote_to_library(self, *, experiment_id: str, strategy_id: str,
                           session_id: str, name: str = "") -> dict:
        """实验产物晋升入策略库（决策 8：唯一的门在入库——必须完整实验血缘）。

        规则：实验必须已 verdicted 且 final_verdict=confirmed；
        版本带 parent_version_id（其 diff 的基准）+ experiment_id 血缘。
        入会话不设 human 门（2026-09-24 用户决策：AI 全流程闭环自动晋升
        ——提议→跑→confirm→晋升；K3-R2-注记-3 的疑虑由血缘门
        verdicted+confirmed 承担）。
        """
        from portfolio import library
        from portfolio import service as portfolio_service
        from research import verdict as verdict_mod

        exp = lifecycle.require_experiment(self.db, experiment_id)
        if exp["status"] != "verdicted":
            raise ResearchError(f"only verdicted experiments can be promoted (status={exp['status']})")
        latest = verdict_mod.latest_verdict(self.db, experiment_id)
        if latest is None or latest["final_verdict"] != "confirmed":
            raise ResearchError(
                "promotion requires final_verdict=confirmed (the gate lives at library entry)"
            )
        if exp["evaluation_module"] != "portfolio_backtest@1":
            raise ResearchError("promotion currently supports portfolio_backtest experiments only")
        spec = loads(exp.get("spec_json"), {})
        base_ref = str(spec.get("base") or "")
        config, _yaml = portfolio_service.resolve_experiment_config(
            self.db, base_version_id=base_ref, diff=spec.get("diff") or [],
            registry=self.registry, new_name=strategy_id,
            allow_retired=True,  # 晋升既有实验不是"新引用"（ND-4）
        )
        library.ensure_strategy(
            self.db, strategy_id, name=name or strategy_id,
            description=f"晋升自实验 {experiment_id}", created_by="experiment",
        )
        version = library.add_version(
            self.db, strategy_id, config, created_by="experiment",
            experiment_id=experiment_id, parent_version_id=base_ref,
        )
        return version

    # ------------------------------------------------------------------
    # 治理
    # ------------------------------------------------------------------
    def propose_module(self, *, session_id: str, slot: str, name: str, version: int,
                       kind: str, source: str, params_schema: dict | None = None) -> dict:
        draft = modules.propose_module(
            self.db, session_id=session_id, slot=slot, name=name, version=version,
            kind=kind, source=source, params_schema=params_schema,
        )
        if draft["status"] == "reviewed":
            modules.load_reviewed_modules(self.db, self.registry)
        return draft

    def retire_module(self, *, draft_id: str, session_id: str) -> dict:
        return modules.retire_module(self.db, draft_id=draft_id, session_id=session_id)

    def recompute_campaign(self, *, old_module_ref: str, new_module_ref: str,
                           session_id: str, limit: int | None = None) -> dict:
        """批量复核（§6.6.7）：仅 human session 可发起（治理动作）。"""
        from research.recompute import recompute_campaign as _campaign

        sessions.require_human_session(self.db, session_id)
        return _campaign(
            self.db, old_module_ref=old_module_ref, new_module_ref=new_module_ref,
            registry=self.registry, session_id=session_id, limit=limit,
        )

    def grant_holdout(self, *, session_id: str, purpose: str,
                      experiment_id: str | None = None) -> dict:
        """holdout 放行（仅 human session；触碰必留痕 + 计数）。"""
        return holdout.grant_token(
            self.db, session_id=session_id, purpose=purpose, experiment_id=experiment_id
        )

    def stale_experiments(self, days: int = 7) -> list[dict]:
        return lifecycle.list_stale_evaluating(self.db, days=days)

    # ------------------------------------------------------------------
    def _materialize(self, topic_id: str) -> None:
        """课题文件夹物化（失败不阻断主流程——文件夹可由 DB 再生成）。"""
        try:
            from research.topic_files import materialize_topic

            materialize_topic(self.db, topic_id, root=self.topics_dir)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("topic materialize failed: %s", exc)
