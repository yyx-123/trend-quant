"""R3 probe 3: 策略库/复现两条纪律路径。

A) 已关课题的复现通道：R2-P3-2 给 rerun 加了 require_open_topic；本探针核验
   "关题后还有没有任何通道能重跑同 spec"（复现 = spec 本身，§6.4.1）。
B) 晋升血缘（决策 8：入库是唯一的门、必须完整实验血缘）：两个实验解析出同一
   份 resolved config（窗口不同 → 不是重复实验）时，第二次晋升是否真的落血
   new experiment_id。**不写生产库**（tmp 目录）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.storage.db import Database  # noqa: E402
from portfolio import library  # noqa: E402
from portfolio.seed import seed_default_library  # noqa: E402
from portfolio.slots import REGISTRY, ensure_builtins  # noqa: E402
from research import experiments, lifecycle, sessions, topics, verdict  # noqa: E402
from research.api import ResearchService  # noqa: E402
from research.errors import ResearchError  # noqa: E402

DIFF = [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.5}}]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe3-"))
    db = Database(tmp / "t.db")
    ensure_builtins()
    versions = seed_default_library(db, REGISTRY)
    base = versions["base-v1"]
    svc = ResearchService(db, registry=REGISTRY, topics_dir=tmp / "topics")
    sess = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(db, session_id=sess["session_id"], title="T", question="q")

    def propose(tag, window, extra=None):
        spec = {"base": base, "diff": DIFF, "window": window, **(extra or {})}
        exp = experiments.propose_experiment(
            db, session_id=sess["session_id"], title=tag, topic_id=topic["id"],
            evaluation_module="portfolio_backtest@1", spec=spec,
            hypothesis="假设陈述足够长用于探针", registry=REGISTRY, allow_duplicate=True,
        )
        lifecycle.transition(db, exp["id"], "running")
        verdict.insert_platform_verdict(
            db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
            report={}, suggested_verdict="confirmed",
        )
        lifecycle.transition(db, exp["id"], "evaluating")
        verdict.confirm_verdict(
            db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="探针确认", session_id=sess["session_id"],
        )
        return exp["id"]

    e1 = propose("E1 window2016", ["2016-01-01", "2024-12-31"])
    e2 = propose("E2 window2017", ["2017-01-01", "2024-12-31"])

    print("=== A) 关题后的复现通道 ===")
    svc.conclude_topic(topic_id=topic["id"], conclusion="结论", session_id=sess["session_id"])
    try:
        r = svc.rerun_experiment(experiment_id=e1, session_id=sess["session_id"])
        print("  rerun ALLOWED:", r["id"])
    except ResearchError as exc:
        print(f"  rerun BLOCKED: {type(exc).__name__}: {exc}")
    # 替代通道 1：新课题重提同 spec
    t2 = topics.create_topic(db, session_id=sess["session_id"], title="T2", question="复现尝试")
    try:
        exp = experiments.propose_experiment(
            db, session_id=sess["session_id"], title="重提同 spec", topic_id=t2["id"],
            evaluation_module="portfolio_backtest@1",
            spec={"base": base, "diff": DIFF, "window": ["2016-01-01", "2024-12-31"]},
            hypothesis="假设陈述足够长用于探针", registry=REGISTRY,
        )
        print("  新课题重提 ALLOWED:", exp["id"])
    except ResearchError as exc:
        print(f"  新课题重提 BLOCKED: {type(exc).__name__}: {exc}")
    # 替代通道 2：append（注解建议的迁移路径）只允许 queued
    try:
        svc.append_experiment_to_topic(experiment_id=e1, topic_id=t2["id"],
                                       session_id=sess["session_id"])
        print("  append ALLOWED")
    except ResearchError as exc:
        print(f"  append BLOCKED: {type(exc).__name__}: {exc}")

    print("=== B) 晋升血缘 ===")
    t3 = topics.create_topic(db, session_id=sess["session_id"], title="T3", question="晋升")
    ids = []
    for i, win in enumerate((["2018-01-01", "2024-12-31"], ["2019-01-01", "2024-12-31"])):
        spec = {"base": base, "diff": DIFF, "window": win}
        exp = experiments.propose_experiment(
            db, session_id=sess["session_id"], title=f"P{i}", topic_id=t3["id"],
            evaluation_module="portfolio_backtest@1", spec=spec,
            hypothesis="假设陈述足够长用于探针", registry=REGISTRY, allow_duplicate=True,
        )
        lifecycle.transition(db, exp["id"], "running")
        verdict.insert_platform_verdict(
            db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
            report={}, suggested_verdict="confirmed",
        )
        lifecycle.transition(db, exp["id"], "evaluating")
        verdict.confirm_verdict(db, experiment_id=exp["id"], final_verdict="confirmed",
                                reasoning="ok", session_id=sess["session_id"])
        ids.append(exp["id"])
    v1 = svc.promote_to_library(experiment_id=ids[0], strategy_id="promo",
                               session_id=sess["session_id"])
    v2 = svc.promote_to_library(experiment_id=ids[1], strategy_id="promo",
                               session_id=sess["session_id"])
    print(f"  promote#1 E={ids[0]} -> {v1['id']} experiment_id={v1['experiment_id']}")
    print(f"  promote#2 E={ids[1]} -> {v2['id']} experiment_id={v2['experiment_id']}")
    print(f"  库里 promo 的版本行数={len(library.list_versions(db, 'promo'))} "
          f"（第二份实验血缘是否落库："
          f"{any(v['experiment_id'] == ids[1] for v in library.list_versions(db, 'promo'))}）")
    print("  resolved config 相同？",
          library.get_version(db, v1["id"])["config_hash"]
          == library.get_version(db, v2["id"])["config_hash"])
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
