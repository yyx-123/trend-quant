"""R3 probe 8: 台账 Web 通道（XSS / 404 / 409 / CSRF）真实 HTTP 走一遍。

独立脚本（不入仓库测试树）：自建 tmp 库 + 打桩 get_db + TestClient 登录。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TREND_QUANT_LOG_DIR", "logs/test")
os.environ["TREND_QUANT_DISABLE_SCHEDULER"] = "1"

XSS = "<script>alert(1)</script>"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r3probe8-"))
    from data.storage.db import Database
    import data.storage.db as db_module

    db = Database(tmp / "t.db")
    db_module.get_db = lambda: db
    db_module._db_instance = db
    db_module.init_db = lambda db_path=None: db

    from portfolio.slots import REGISTRY, ensure_builtins
    from research import lifecycle, sessions, topics, verdict
    from research.api import ResearchService

    ensure_builtins()
    from portfolio.seed import seed_default_library

    versions = seed_default_library(db, REGISTRY)
    svc = ResearchService(db, registry=REGISTRY, topics_dir=tmp / "topics")
    s = sessions.get_or_create_default_human_session(db)
    topic = topics.create_topic(db, session_id=s["session_id"], title=f"T {XSS}",
                                question="q <b>x</b>")
    exp = __import__("research.experiments", fromlist=["x"]).propose_experiment(
        db, session_id=s["session_id"], title="实验", topic_id=topic["id"],
        evaluation_module="portfolio_backtest@1",
        spec={"base": versions["base-v1"], "diff": [{"slot": "position_risk", "to": "hard_stop@1", "params": {"atr_mul": 1.5}}]},
        hypothesis="假设陈述足够长用于探针", registry=REGISTRY, allow_duplicate=True,
    )
    lifecycle.transition(db, exp["id"], "running")
    verdict.insert_platform_verdict(
        db, experiment_id=exp["id"], baseline={}, evidence={},
        warnings=[f"w {XSS}"], report={}, suggested_verdict="confirmed",
    )
    lifecycle.transition(db, exp["id"], "evaluating")

    from fastapi.testclient import TestClient

    import app.main as app_main

    app_main.app.state.research_service = svc
    app_main.app.state.research_worker = None
    db.create_user("tester", "pw-tester")
    with TestClient(app_main.app) as c:
        c.headers.update({"X-Requested-With": "XMLHttpRequest"})
        r = c.post("/api/auth/login", json={"username": "tester", "password": "pw-tester"})
        print("login:", r.status_code)
        board = c.get("/research-ledger")
        print("board:", board.status_code)
        html = board.text
        print("  原样 script 标签出现 =", XSS in html)
        print("  转义形态出现 =", "&lt;script&gt;" in html)
        d = c.get(f"/research-ledger/experiments/{exp['id']}")
        print("detail:", d.status_code, "| 警告转义 =", "&lt;script&gt;" in d.text)
        print("  渲染 final 列 =", "confirmed" in d.text)
        print("missing exp:", c.get("/research-ledger/experiments/E9999").status_code)
        print("report.json(有 verdict):",
              c.get(f"/research-ledger/experiments/{exp['id']}/report.json").status_code)
        # CSRF / 跨站
        print("confirm cross-site:",
              c.post(f"/research-ledger/experiments/{exp['id']}/confirm",
                     data={"final_verdict": "confirmed", "reasoning": "x"},
                     headers={"Sec-Fetch-Site": "cross-site",
                              "Origin": "https://evil.example"}).status_code)
        print("conclude cross-site:",
              c.post("/research-ledger/topics/conclude",
                     data={"topic_id": topic["id"], "conclusion": "c"},
                     headers={"Origin": "https://evil.example"}).status_code)
        print("holdout grant cross-site:",
              c.post("/research-ledger/holdout/grant", data={"purpose": "p"},
                     headers={"Origin": "https://evil.example"}).status_code)
        # 同源：confirm 一个不在 evaluating 的实验 → 409
        print("confirm non-evaluating:",
              c.post("/research-ledger/experiments/E9999/confirm",
                     data={"final_verdict": "confirmed", "reasoning": "x"}).status_code)
        # 同源合法 confirm
        ok = c.post(f"/research-ledger/experiments/{exp['id']}/confirm",
                    data={"final_verdict": "confirmed", "reasoning": "理由"},
                    follow_redirects=False)
        print("confirm same-origin:", ok.status_code, ok.headers.get("location"))
        print("holdout grant empty purpose:",
              c.post("/research-ledger/holdout/grant", data={"purpose": "  "}).status_code)
        print("holdout grant ok:",
              c.post("/research-ledger/holdout/grant",
                     data={"purpose": "探针", "experiment_id": exp["id"]}).status_code)
        print("ledger 记录数:",
              db.connect().__enter__().execute(
                  "SELECT COUNT(*) FROM holdout_tokens").fetchone()[0])
    print("TMP", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
