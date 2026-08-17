from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.calendar import market_now, trading_session_status
from data.storage.db import get_db
from services.dashboard import RevisionCache, build_subject_dashboard_payload
from services.dashboard_snapshot import snapshot_runner

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
    """快照优先：有当日盘中快照就返回快照，否则返回 EOD 看板。

    EOD 数据已覆盖今日（收盘补库落库）时以盘后确认值为准，忽略快照。
    """
    db = get_db()
    revision = db.get_market_dashboard_revision()
    today = market_now().date().isoformat()
    max_bar = str(revision[0])[:10] if revision and revision[0] else ""
    if max_bar < today:
        snapshot = snapshot_runner.latest_snapshot()
        if snapshot and snapshot.get("as_of") == today:
            return {**snapshot["payload"], "snapshot_ts": snapshot["computed_at"]}
    return _dashboard_cache.get_or_compute(revision, lambda: build_subject_dashboard_payload(db))


@router.get("/api/trading-status")
async def get_trading_status() -> dict:
    """Return current A-share trading session status."""
    return trading_session_status()


@router.post("/api/dashboard/refresh")
async def refresh_dashboard() -> dict:
    """触发一次盘中实时重算（页面打开时由前端调用）。

    与定时任务共用同一单例入口：已有任务在跑时返回 running，不重复启动；
    非交易时段 / 盘前 / 今日日K已落库时返回 skipped。
    """
    return snapshot_runner.ensure_running(trigger="page_open")


@router.get("/api/dashboard/refresh-status")
async def refresh_status() -> dict:
    """轮询重算进度；snapshot_ts 变化即代表有新快照可取。"""
    return snapshot_runner.status()
