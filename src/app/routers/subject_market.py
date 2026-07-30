from __future__ import annotations

import logging
import threading
import time as _time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.calendar import is_realtime_available, is_trading_day, trading_session_status
from core.display import filter_fully_classified
from data.intraday_service import build_intraday_dashboard
from data.service import DataService
from data.storage.db import get_db
from services.dashboard import RevisionCache, build_subject_dashboard_payload
from services.market_indicators import trend_config as _trend_config

router = APIRouter(prefix="/subject-market", tags=["subject-market"])
templates = Jinja2Templates(directory="web/templates")
logger = logging.getLogger(__name__)

_dashboard_cache = RevisionCache()


def warm_dashboard_cache() -> None:
    """Pre-compute the EOD dashboard payload (called from app lifespan hooks).

    Fills the same RevisionCache the endpoint reads, so the first user hit
    after startup or after the daily 16:30 update is served from cache.
    """
    db = get_db()
    revision = db.get_market_dashboard_revision()
    _dashboard_cache.get_or_compute(revision, lambda: build_subject_dashboard_payload(db))


@router.get("", response_class=HTMLResponse)
async def subject_market_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="subject_market.html", request=request, context={"title": "标的看板"}
    )


@router.get("/api/dashboard")
async def subject_market_dashboard() -> dict:
    db = get_db()
    revision = db.get_market_dashboard_revision()
    return _dashboard_cache.get_or_compute(revision, lambda: build_subject_dashboard_payload(db))


# ---------------------------------------------------------------------------
# Intraday (real-time) endpoints
# ---------------------------------------------------------------------------

_intraday_jobs: dict[str, dict[str, Any]] = {}
_intraday_jobs_lock = threading.Lock()
_INTRADAY_JOB_TTL_SECONDS = 300  # 5 minutes


def _cleanup_expired_jobs() -> None:
    """Remove intraday jobs older than TTL."""
    now = _time.monotonic()
    with _intraday_jobs_lock:
        expired = [
            jid for jid, job in _intraday_jobs.items()
            if now - job.get("_created", now) > _INTRADAY_JOB_TTL_SECONDS
        ]
        for jid in expired:
            del _intraday_jobs[jid]


def _create_intraday_job() -> str:
    _cleanup_expired_jobs()
    job_id = uuid.uuid4().hex[:12]
    with _intraday_jobs_lock:
        _intraday_jobs[job_id] = {
            "status": "started",
            "percent": 0.0,
            "message": "正在准备…",
            "result": None,
            "_created": _time.monotonic(),
        }
    return job_id


@router.get("/api/trading-status")
async def get_trading_status() -> dict:
    """Return current A-share trading session status."""
    return trading_session_status()


@router.post("/api/intraday-dashboard/start")
async def start_intraday_dashboard() -> dict:
    """Launch an intraday dashboard computation in the background."""
    now = datetime.now()
    if not is_trading_day(now.date()):
        raise HTTPException(status_code=400, detail="今日非交易日")
    # 午间休盘（11:30-13:00）也允许启动 —— 实时报价在休盘期间仍然有效。
    if not is_realtime_available(now):
        raise HTTPException(status_code=400, detail="当前非交易时段（9:30-15:00，含午间休盘）")

    job_id = _create_intraday_job()

    def _run() -> None:
        try:
            db = get_db()
            symbols = db.list_market_symbols(price_mode="qfq")
            if not symbols:
                with _intraday_jobs_lock:
                    _intraday_jobs[job_id]["status"] = "error"
                    _intraday_jobs[job_id]["message"] = "本地无日K数据"
                return

            # Filter to fully classified instruments only.
            metadata_map = db.get_instrument_metadata_map()
            classified = filter_fully_classified(symbols, metadata_map)
            if not classified:
                with _intraday_jobs_lock:
                    _intraday_jobs[job_id]["status"] = "error"
                    _intraday_jobs[job_id]["message"] = "无完整分类的标的"
                return

            data_service = DataService()
            cfg = _trend_config()

            def on_progress(update: dict) -> None:
                with _intraday_jobs_lock:
                    job = _intraday_jobs.get(job_id)
                    if job:
                        job["percent"] = float(update.get("percent", 0))
                        job["message"] = str(update.get("message", ""))

            payload = build_intraday_dashboard(
                classified,
                db=db,
                data_service=data_service,
                trend_config=cfg,
                progress_callback=on_progress,
            )
            with _intraday_jobs_lock:
                job = _intraday_jobs.get(job_id)
                if job:
                    job["status"] = "done"
                    job["percent"] = 1.0
                    job["message"] = "完成"
                    job["result"] = payload
        except Exception as exc:
            logger.exception("Intraday dashboard job_id=%s failed", job_id)
            with _intraday_jobs_lock:
                job = _intraday_jobs.get(job_id)
                if job:
                    job["status"] = "error"
                    job["message"] = str(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "started"}


@router.get("/api/intraday-dashboard/progress/{job_id}")
async def get_intraday_dashboard_progress(job_id: str) -> dict:
    """Poll the progress of an intraday dashboard job."""
    with _intraday_jobs_lock:
        job = _intraday_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "job_id": job_id,
        "status": job["status"],
        "percent": job["percent"],
        "message": job["message"],
    }


@router.get("/api/intraday-dashboard/result/{job_id}")
async def get_intraday_dashboard_result(job_id: str) -> dict:
    """Retrieve the result of a completed intraday dashboard job."""
    with _intraday_jobs_lock:
        job = _intraday_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] == "error":
        raise HTTPException(status_code=500, detail=job.get("message", "盘中计算失败"))
    if job["status"] != "done":
        raise HTTPException(status_code=202, detail="任务尚未完成")
    return job["result"]
