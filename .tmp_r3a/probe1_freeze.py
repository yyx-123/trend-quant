"""R3 probe 1: 同步通道的 run_freeze 覆盖面（CLI rerun --run / CLI recompute）。

对照：CLI propose-experiment --run 与 MCP 同步路径都显式 frozen_writes()；
本探针检查 rerun --run 与 recompute 是否同样包裹。

方法：进程内调用 scripts/research_cli.py 的 main()，把真正执行 run 的入口
（research.pipeline.run_experiment / 评估模块 runner）替换为"记录当时是否冻结"
的间谍——不跑真回测。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import core.run_freeze as rf  # noqa: E402
from data.storage.db import Database  # noqa: E402
from portfolio.slots import REGISTRY, ensure_builtins  # noqa: E402
from research import lifecycle, sessions, topics, verdict  # noqa: E402
from research.evaluations import EvaluationModule, register_evaluation  # noqa: E402

observed: list[dict] = []


def _spy_runner(db, exp, ctx):
    observed.append({"where": "evaluation.runner", "frozen": rf.is_frozen()})
    return {
        "baseline": {}, "evidence": {}, "warnings": [], "report": {},
        "suggested_verdict": "confirmed",
        "runs": [{"engine_run_id": None, "window_start": "2015-01-01",
                  "window_end": "2024-12-31", "window_kind": "sample",
                  "holdout_touched": False}],
    }


def load_cli():
    spec = importlib.util.spec_from_file_location(
        "research_cli_probe", ROOT / "scripts" / "research_cli.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(cli, argv):
    old = sys.argv
    sys.argv = ["research-cli", *argv]
    try:
        return cli.main()
    finally:
        sys.argv = old


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe1-"))
    db_path = tmp / "t.db"
    db = Database(db_path)
    ensure_builtins()
    register_evaluation(EvaluationModule(
        name="probe_backtest", version=1, description="probe",
        validate_spec=lambda spec, ctx: [],
        subject_key=lambda spec: str(spec.get("base") or ""),
        runner=_spy_runner,
    ))
    from portfolio.seed import seed_default_library

    versions = seed_default_library(db, REGISTRY)
    sess = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(db, session_id=sess["session_id"], title="冻结探针", question="q")

    def make_verdicted(idx: int, old_ref: str = "hard_stop@1") -> str:
        exp = __import__("research.experiments", fromlist=["x"]).propose_experiment(
            db, session_id=sess["session_id"], title=f"e{idx}", topic_id=topic["id"],
            evaluation_module="probe_backtest@1",
            spec={"base": versions["base-v1"],
                  "diff": [{"slot": "position_risk", "to": old_ref, "params": {"atr_mul": 1.5}}],
                  "window": ["2015-01-01", "2024-12-31"], "probe_tag": idx},
            hypothesis="假设陈述足够长用于探针", registry=REGISTRY,
            allow_duplicate=True,
        )
        lifecycle.transition(db, exp["id"], "running")
        verdict.insert_platform_verdict(
            db, experiment_id=exp["id"], baseline={}, evidence={}, warnings=[],
            report={}, suggested_verdict="confirmed",
        )
        lifecycle.transition(db, exp["id"], "evaluating")
        verdict.confirm_verdict(
            db, experiment_id=exp["id"], final_verdict="confirmed",
            reasoning="probe", session_id=sess["session_id"],
        )
        return exp["id"]

    target = make_verdicted(1)
    make_verdicted(2, old_ref="hard_stop@1")
    cli = load_cli()

    for argv in (
        ["--db", str(db_path), "rerun", target, "--run"],
        ["--db", str(db_path), "recompute", "--old", "hard_stop@1", "--new", "hard_stop@2"],
    ):
        observed.clear()
        try:
            rc = run_cli(cli, argv)
        except SystemExit as exc:
            rc = f"SystemExit({exc.code})"
        print(f"argv={argv[3:]} rc={rc} observed={observed}")

    # 对照：MCP 同步路径（_run_or_queue，无 worker）在同一上下文里的冻结状态
    from research import pipeline

    observed.clear()
    exp3 = __import__("research.experiments", fromlist=["x"]).propose_experiment(
        db, session_id=sess["session_id"], title="e3", topic_id=topic["id"],
        evaluation_module="probe_backtest@1",
        spec={"base": versions["base-v1"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.5}}],
              "window": ["2015-01-01", "2024-12-31"], "probe_tag": 3},
        hypothesis="假设陈述足够长用于探针", registry=REGISTRY, allow_duplicate=True,
    )
    with rf.frozen_writes():
        pipeline.run_experiment(db, exp3["id"], registry=REGISTRY)
    print(f"control(MCP-style frozen_writes + pipeline) observed={observed}")
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
