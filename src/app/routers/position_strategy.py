from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from audit.app_logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/position-strategies", tags=["position-strategies"])
from core.paths import web_dir as _web_dir

_templates_dir = _web_dir() / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@router.get("", response_class=HTMLResponse)
async def position_strategies_page(request: Request) -> HTMLResponse:
    """仓位策略管理页 (CRUD reuses /rule-backtest/api/position-strategies*)."""
    return templates.TemplateResponse(
        name="position_strategies.html",
        request=request,
        context={"title": "仓位策略"},
    )
