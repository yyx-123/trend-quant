from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/position-strategies", tags=["position-strategies"])
templates = Jinja2Templates(directory="web/templates")


@router.get("", response_class=HTMLResponse)
async def position_strategies_page(request: Request) -> HTMLResponse:
    """仓位策略管理页 (CRUD reuses /rule-backtest/api/position-strategies*)."""
    return templates.TemplateResponse(
        name="position_strategies.html",
        request=request,
        context={"title": "仓位策略"},
    )
