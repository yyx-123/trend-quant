"""R3 probe 7: (A) MCP 12 工具真实调用 + 权限边界；(B) 课题物化的确定性与诚实性。

A：用假 mcp 收集 register_research_tools 注册的 12 个函数（穿透装饰器直接调用
   真函数体），把 service 挂到 app.state（避免落到生产库）。
B：同一 DB 物化两次比对字节；再核对物化产物与 DB 的 verdict 字段是否一致。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.storage.db import Database  # noqa: E402
from portfolio.seed import seed_default_library  # noqa: E402
from portfolio.slots import REGISTRY, ensure_builtins  # noqa: E402
from research import experiments, lifecycle, sessions, topics, verdict  # noqa: E402
from research.api import ResearchService  # noqa: E402


class _FakeMcp:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe7-"))
    db = Database(tmp / "t.db")
    ensure_builtins()
    versions = seed_default_library(db, REGISTRY)
    svc = ResearchService(db, registry=REGISTRY, topics_dir=tmp / "topics")

    import app.main as app_main

    app_main.app.state.research_service = svc
    app_main.app.state.research_worker = None

    from trend_mcp.research_tools import register_research_tools

    fake = _FakeMcp()
    register_research_tools(fake)
    print("=== A) MCP 工具面 ===")
    print("  注册工具数 =", len(fake.tools), sorted(fake.tools))
    print("  grant_holdout 是否暴露 =", any("holdout" in n for n in fake.tools))

    r = fake.tools["research_propose_topic"](title="AI课题", question="q")
    tid = r["topic"]["id"]
    r = fake.tools["research_propose_experiment"](
        topic_id=tid, evaluation_module="portfolio_backtest@1",
        spec={"base": versions["base-v1"],
              "diff": [{"slot": "position_risk", "to": "hard_stop@1",
                        "params": {"atr_mul": 1.5}}]},
        hypothesis="MCP 通道的假设陈述足够长", run=False,
    )
    eid = r.get("experiment_id")
    print("  propose_experiment ->", {k: r.get(k) for k in ("ok", "experiment_id", "status")})
    # 未到 evaluating 就 confirm → 分类是否清晰
    r = fake.tools["research_confirm_verdict"](experiment_id=eid, final_verdict="confirmed",
                                              reasoning="x")
    print("  confirm(queued) ->", r)
    # 不存在的实验
    print("  get_experiment(不存在) ->", fake.tools["research_get_experiment"](experiment_id="E9999"))
    print("  run_status(不存在) ->", fake.tools["research_run_status"](experiment_id="E9999"))
    print("  search_ledger ->", fake.tools["research_search_ledger"]()["results"][-1]
          if fake.tools["research_search_ledger"]()["results"] else "empty")
    print("  propose_module(bad slot) ->", fake.tools["research_propose_module"](
        slot="nope", name="m", version=1, kind="dsl", source="close"))
    print("  promote(未确认) ->", fake.tools["research_promote_to_library"](
        experiment_id=eid, strategy_id="ai-line"))
    print("  list_topics ->", len(fake.tools["research_list_topics"]()["topics"]), "个")
    print("  register_module_catalog ->", len(
        fake.tools["research_register_module_catalog"]()["evaluations"]), "个评估模块")
    print("  conclude(未终态) ->", fake.tools["research_conclude_topic"](
        topic_id=tid, conclusion="过早结论")["error"][:60])
    print("  rerun(queued) ->", fake.tools["research_rerun_experiment"](experiment_id=eid))

    print("=== B) 课题物化 ===")
    s = sessions.get_or_create_default_human_session(db)
    # 走真实终局：running → evaluating → verdict → confirm
    lifecycle.transition(db, eid, "running")
    verdict.insert_platform_verdict(
        db, experiment_id=eid, baseline={"sharpe": 1.0},
        evidence={"deltas_vs_base": {"delta_sharpe": 0.5}}, warnings=["w1"],
        report={"experiment_summary": {"sharpe": 1.1}}, suggested_verdict="confirmed",
    )
    lifecycle.transition(db, eid, "evaluating")
    svc.confirm_verdict(experiment_id=eid, final_verdict="confirmed",
                        reasoning="确认理由文本", session_id=s["session_id"])
    svc.conclude_topic(topic_id=tid, conclusion="课题结论", session_id=s["session_id"])
    tdir = tmp / "topics"
    d1 = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tdir.rglob("*")) if p.is_file()}
    from research.topic_files import materialize_topic

    materialize_topic(db, tid, root=tdir)
    d2 = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tdir.rglob("*")) if p.is_file()}
    print("  文件 =", [str(p.relative_to(tdir)) for p in d1])
    print("  二次物化字节一致 =", d1 == d2)
    rep = json.loads((next(p for p in d1 if p.name == "report.json")).read_text(encoding="utf-8"))
    man = json.loads((next(p for p in d1 if p.name == "manifest.json")).read_text(encoding="utf-8"))
    md = next(p for p in d1 if p.name == "REPORT.md").read_text(encoding="utf-8")
    print("  report.json 顶层键 =", sorted(rep))
    print("  report.json 含定论/理由 =", ("final_verdict" in rep, "reasoning" in rep))
    print("  REPORT.md 含理由 =", "确认理由文本" in md, "| 含 final =", "confirmed" in md)
    print("  manifest 键 =", sorted(man))
    print("  manifest.data_version =", man["data_version"], " engine_version =", man["engine_version"])
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
