"""研究 CLI 通道（薄通道厚服务；低频操作走脚本——不写页面入口）。

用法（在项目根目录）：
    .venv/Scripts/python.exe scripts/research_cli.py topics
    .venv/Scripts/python.exe scripts/research_cli.py propose-topic --title 止损 --question "硬止损还是吊灯？"
    .venv/Scripts/python.exe scripts/research_cli.py propose-experiment --topic T001 \
        --eval portfolio_backtest@1 --spec '{"base": "base-v1@1", "diff": [...]}' \
        --hypothesis "..." --run
    .venv/Scripts/python.exe scripts/research_cli.py ledger --subject base-v1
    .venv/Scripts/python.exe scripts/research_cli.py confirm E0001 --verdict confirmed --reasoning "..."
    .venv/Scripts/python.exe scripts/research_cli.py conclude T001 --conclusion "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _service(db_path=None):
    from data.storage.db import Database
    from portfolio.slots import REGISTRY, ensure_builtins
    from research.api import ResearchService

    ensure_builtins()
    return ResearchService(Database(db_path) if db_path else Database(), registry=REGISTRY)


def main() -> int:
    parser = argparse.ArgumentParser(prog="research-cli")
    # DS-复审-R2 §4-8：--db 指定库（默认直连生产库——低频运维需要能指向
    # 测试库/备份库，防误操作默认库）
    parser.add_argument("--db", default=None, help="数据库路径（缺省 = 默认库）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("topics")
    p = sub.add_parser("propose-topic")
    p.add_argument("--title", required=True)
    p.add_argument("--question", required=True)

    p = sub.add_parser("propose-experiment")
    p.add_argument("--topic", required=True)
    p.add_argument("--eval", dest="evaluation_module", required=True)
    p.add_argument("--spec", required=True, help="JSON 字符串")
    p.add_argument("--hypothesis", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--allow-duplicate", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--token", default=None, help="holdout 放行 token（触碰样本外时需要）")

    p = sub.add_parser("ledger")
    p.add_argument("--subject", default="")
    p.add_argument("--topic", default="")
    p.add_argument("--verdict", default="")

    p = sub.add_parser("confirm")
    p.add_argument("experiment_id")
    p.add_argument("--verdict", required=True, choices=["confirmed", "rejected", "inconclusive"])
    p.add_argument("--reasoning", required=True)

    p = sub.add_parser("rerun")
    p.add_argument("experiment_id")
    p.add_argument("--run", action="store_true",
                   help="复现实验入队后立即同步执行（R1-P3-10：纯 CLI 用法不再停在 queued）")
    p.add_argument("--token", default=None, help="holdout 放行 token（复现触碰样本外窗口时需要）")

    p = sub.add_parser("recompute")
    p.add_argument("--old", dest="old_ref", required=True)
    p.add_argument("--new", dest="new_ref", required=True)
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("promote")
    p.add_argument("experiment_id")
    p.add_argument("--strategy-id", required=True)
    p.add_argument("--name", default="")

    p = sub.add_parser("conclude")
    p.add_argument("topic_id")
    p.add_argument("--conclusion", required=True)
    p.add_argument("--grade", default="")

    args = parser.parse_args()
    service = _service(args.db)
    session = service.default_human_session()

    if args.cmd == "topics":
        for t in service.list_topics():
            print(f"{t['id']} [{t['status']}] {t['title']} — {t.get('conclusion') or ''}")
        return 0
    if args.cmd == "propose-topic":
        topic = service.propose_topic(
            session_id=session["session_id"], title=args.title, question=args.question
        )
        print(json.dumps(topic, ensure_ascii=False))
        return 0
    if args.cmd == "propose-experiment":
        try:
            exp = service.propose_experiment(
                session_id=session["session_id"], title=args.title or args.evaluation_module,
                topic_id=args.topic, evaluation_module=args.evaluation_module,
                spec=json.loads(args.spec), hypothesis=args.hypothesis,
                allow_duplicate=args.allow_duplicate, auto_queue=False,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc),
                              "reasons": getattr(exc, "reasons", None)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "experiment_id": exp["id"], "status": exp["status"]},
                         ensure_ascii=False))
        if args.run:
            from core import run_freeze
            from research.pipeline import run_experiment

            # 冻结写包裹（GLM53F-P2-11）：同步通道与 worker 同口径——run 期间
            # 日更写任务冻结，防"一次 run 读到两版 qfq"（决策 A3）
            with run_freeze.frozen_writes():
                result = run_experiment(service.db, exp["id"], registry=service.registry,
                                        holdout_token=getattr(args, "token", None))
            print(json.dumps({"status": result.get("status"),
                              "suggested": (result.get("suggested_verdict"))}, ensure_ascii=False))
        return 0
    if args.cmd == "ledger":
        for row in service.search_ledger(
            subject_key=args.subject or None, topic_id=args.topic or None,
            final_verdict=args.verdict or None,
        ):
            print(f"{row['id']} [{row['status']}] {row['title']} → {row.get('final_verdict')}")
        return 0
    if args.cmd == "confirm":
        try:
            v = service.confirm_verdict(
                experiment_id=args.experiment_id, final_verdict=args.verdict,
                reasoning=args.reasoning, session_id=session["session_id"],
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "final": v["final_verdict"]}, ensure_ascii=False))
        return 0
    if args.cmd == "rerun":
        try:
            exp = service.rerun_experiment(
                experiment_id=args.experiment_id, session_id=session["session_id"],
                auto_queue=False,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        out = {"ok": True, "reproduction_of": args.experiment_id,
               "experiment_id": exp["id"],
               "attempt_index": exp["attempt_index"]}
        if getattr(args, "run", False):
            from core import run_freeze
            from research.pipeline import run_experiment

            try:
                # R3C-P3-1（Round 3 复核）：与 propose --run / MCP 同步路径 /
                # worker 同口径——研究 run 一律在冻结写任务的保护下执行
                with run_freeze.frozen_writes():
                    v = run_experiment(
                        service.db, exp["id"], registry=service.registry,
                        holdout_token=getattr(args, "token", None),
                    )
            except Exception as exc:
                out["run_error"] = str(exc)
            else:
                out["final_verdict"] = v.get("final_verdict")
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "recompute":
        from core import run_freeze

        try:
            with run_freeze.frozen_writes():  # 同 rerun --run（R3C-P3-1）
                result = service.recompute_campaign(
                    old_module_ref=args.old_ref, new_module_ref=args.new_ref,
                    session_id=session["session_id"], limit=args.limit,
                )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        # R3C-P3-3：skipped 必须可见（"哪些目标没复核"是 R1-P3-18 的修复面，
        # 此前只在返回值里、CLI 恰好是唯一能发起 campaign 的通道）
        print(json.dumps({"ok": True, "recomputed": len(result["recomputed"]),
                          "failed": len(result["failed"]),
                          "skipped": result.get("skipped", [])}, ensure_ascii=False))
        return 0
    if args.cmd == "promote":
        try:
            v = service.promote_to_library(
                experiment_id=args.experiment_id, strategy_id=args.strategy_id,
                session_id=session["session_id"], name=args.name,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "version_id": v["id"]}, ensure_ascii=False))
        return 0
    if args.cmd == "conclude":
        try:
            t = service.conclude_topic(
                topic_id=args.topic_id, conclusion=args.conclusion,
                session_id=session["session_id"], grade=args.grade or None,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "grade": t["conclusion_grade"]}, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
