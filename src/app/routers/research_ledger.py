"""台账看板（详设 §4 阶段 5：台账看板 UI；§6.4.1 课题与实验上看板）。

全员只读开放的经验资产：失败/成功实验同等展示。页面操作（确认 verdict /
关题 / holdout 放行）走默认人工会话——登录墙本身就是身份边界（cookie
session），细粒度 RBAC 不是一期范围。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from audit.app_logger import get_logger
from core.paths import web_dir
from data.storage import db as db_module
from research.api import ResearchService
from research.errors import ResearchError

logger = get_logger(__name__)
router = APIRouter(prefix="/research-ledger", tags=["research-ledger"])
templates = Jinja2Templates(directory=str(web_dir() / "templates"))


def _service() -> ResearchService:
    from app.main import app

    service = getattr(app.state, "research_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="research service not initialized")
    return service


def _service_or_testbed() -> ResearchService:
    """测试环境（app.state 未挂载）下直连默认库构造临时服务面。"""
    try:
        return _service()
    except HTTPException:
        from portfolio.slots import REGISTRY, ensure_builtins

        ensure_builtins()
        db = db_module.get_db()
        # 测试/裸上下文：课题文件夹物化到 DB 旁路目录，不污染仓库 research/
        return ResearchService(
            db, registry=REGISTRY, topics_dir=db.db_path.parent / "research_topics"
        )


@router.get("", response_class=HTMLResponse)
def ledger_board(request: Request):
    service = _service_or_testbed()
    topic_rows = service.list_topics()
    experiments = service.list_experiments(include_archived=True)
    stale = service.stale_experiments(days=7)
    live_lists = _recent_live_lists()
    worker_status = _worker_status()
    return templates.TemplateResponse(
        name="research_ledger.html",
        request=request,
        context={
            "title": "投研台账",
            "topics": topic_rows,
            "experiments": [_exp_row(service, e) for e in experiments],
            "stale": stale,
            "live_lists": live_lists,
            "worker_status": worker_status,
        },
    )


def _exp_row(service, exp: dict) -> dict:
    from research.verdict import canonical_verdict

    detail = service.get_experiment(exp["id"]) or exp
    # 定论优先（GLM53F-P1-4）：复核稿不劫持看板展示位
    latest = canonical_verdict(detail.get("verdicts") or [])
    return {
        **exp,
        "spec_pretty": json.dumps(detail.get("spec"), ensure_ascii=False),
        "suggested_verdict": None if latest is None else latest["suggested_verdict"],
        "final_verdict": None if latest is None else latest["final_verdict"],
        "warnings": [] if latest is None else latest.get("warnings", []),
        "verdict_id": None if latest is None else latest["id"],
    }


@router.get("/experiments/{experiment_id}", response_class=HTMLResponse)
def experiment_detail(request: Request, experiment_id: str):
    service = _service_or_testbed()
    detail = service.get_experiment(experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    verdicts = detail.get("verdicts", [])
    return templates.TemplateResponse(
        name="research_experiment.html",
        request=request,
        context={
            "title": f"实验 {experiment_id}",
            "exp": detail,
            "spec_pretty": json.dumps(detail.get("spec"), ensure_ascii=False, indent=2),
            "verdicts": [
                {
                    **v,
                    "evidence_pretty": json.dumps(v.get("evidence"), ensure_ascii=False, indent=2)[:4000],
                    "report_pretty": json.dumps(v.get("report"), ensure_ascii=False, indent=2)[:4000],
                }
                for v in verdicts
            ],
        },
    )


@router.get("/experiments/{experiment_id}/report.json")
def experiment_report_download(experiment_id: str):
    """完整实验报告下载（§6.5.0 报告完整性：页面不许只有截断视图——
    评审 DS-P1-1）"""
    from fastapi.responses import JSONResponse

    service = _service_or_testbed()
    detail = service.get_experiment(experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    from research.verdict import canonical_verdict

    latest = canonical_verdict(detail.get("verdicts") or [])
    if latest is None:
        raise HTTPException(status_code=404, detail="no verdict yet")
    return JSONResponse(
        content={
            "experiment_id": experiment_id,
            "spec": detail.get("spec"),
            "hypothesis": detail.get("hypothesis"),
            "baseline": latest.get("baseline"),
            "evidence": latest.get("evidence"),
            "warnings": latest.get("warnings"),
            "report": latest.get("report"),
            "suggested_verdict": latest.get("suggested_verdict"),
            "final_verdict": latest.get("final_verdict"),
            "reasoning": latest.get("reasoning"),
        }
    )


@router.post("/experiments/{experiment_id}/confirm")
def confirm_verdict(
    experiment_id: str,
    final_verdict: str = Form(...),
    reasoning: str = Form(...),
):
    service = _service_or_testbed()
    session = service.default_human_session()
    try:
        service.confirm_verdict(
            experiment_id=experiment_id, final_verdict=final_verdict,
            reasoning=reasoning, session_id=session["session_id"],
        )
    except (ResearchError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/research-ledger/experiments/{experiment_id}", status_code=303)


@router.post("/topics/{topic_id}/conclude")
def conclude_topic(
    topic_id: str,
    conclusion: str = Form(...),
    grade: str = Form(""),
):
    service = _service_or_testbed()
    session = service.default_human_session()
    try:
        service.conclude_topic(
            topic_id=topic_id, conclusion=conclusion,
            session_id=session["session_id"], grade=grade or None,
        )
    except (ResearchError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/research-ledger", status_code=303)


@router.post("/holdout/grant")
def grant_holdout(purpose: str = Form(""), experiment_id: str = Form("")):
    service = _service_or_testbed()
    session = service.default_human_session()
    try:
        service.grant_holdout(
            session_id=session["session_id"], purpose=purpose,
            experiment_id=experiment_id or None,
        )
    except (ResearchError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/research-ledger", status_code=303)


def _recent_live_lists(limit: int = 10) -> list[dict]:
    with db_module.get_db().connect() as conn:
        rows = conn.execute(
            """SELECT * FROM portfolio_live_lists ORDER BY list_date DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        target = json.loads(d.get("target_json") or "{}")
        d["buys_count"] = len(target.get("buys", []))
        d["sells_count"] = len(target.get("sells", []))
        d["target_json_pretty"] = json.dumps(target, ensure_ascii=False, indent=2)[:3000]
        out.append(d)
    return out


def _worker_status() -> dict | None:
    from app.main import app

    worker = getattr(app.state, "research_worker", None)
    return None if worker is None else worker.status()
