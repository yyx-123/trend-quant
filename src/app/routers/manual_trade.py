from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import auth
from services import trade_records as tr
from services.manual_trade import compute_manual_trade
from services.stop_loss import StopLossError, stop_mode_toggle_title

router = APIRouter(prefix="/manual-trade", tags=["manual-trade"])
from core.paths import web_dir as _web_dir

_templates_dir = _web_dir() / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

StopMode = Literal["tight", "loose"]


class ManualTradeEvaluateRequest(BaseModel):
    symbol: str = Field(..., min_length=1, description="标的代码，如 510300 或 510300.SS")
    buy_date: date = Field(..., description="买入日期")
    buy_price: float = Field(..., gt=0, description="买入均价")
    stop_mode: StopMode | None = Field(
        None, description="止损松紧：tight 紧止损（1×/2×ATR），loose 松止损（1.5×/2.5×ATR）"
    )
    risk_budget: float | None = Field(
        None, gt=0, description="风险预算金额（元）：按硬止损损失推算最大可买入份数（下取整到百位）"
    )


class TradeCreateRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    buy_date: date
    buy_price: float = Field(..., gt=0)
    shares: float = Field(..., gt=0, description="买入份数")


class TradeCloseRequest(BaseModel):
    trade_id: int
    sell_date: date
    sell_price: float = Field(..., gt=0)


class TradeListRequest(BaseModel):
    stop_mode: StopMode | None = Field(None, description="止损松紧：tight 紧止损 / loose 松止损")


@router.get("", response_class=HTMLResponse)
async def manual_trade_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="manual_trade.html", request=request, context={"stop_mode_title": stop_mode_toggle_title(), "title": "手工交易"}
    )


@router.post("/api/evaluate")
async def evaluate_manual_trade(payload: ManualTradeEvaluateRequest) -> dict:
    """试算（不落库；全站登录墙内，无需重复鉴权）。"""
    try:
        return compute_manual_trade(
            payload.symbol,
            payload.buy_date.isoformat(),
            payload.buy_price,
            stop_mode=payload.stop_mode,
            risk_budget=payload.risk_budget,
        )
    except StopLossError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _call_trade_api(fn, *args, **kwargs):
    """交易记录接口的统一错误映射：403 / 400。"""
    try:
        return fn(*args, **kwargs)
    except tr.TradePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (tr.TradeRecordError, StopLossError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/trades/list")
async def list_trades(
    payload: TradeListRequest, user: dict = Depends(auth.get_current_user)
) -> dict:
    return _call_trade_api(tr.list_trades, user, stop_mode=payload.stop_mode)


@router.post("/api/trades/create")
async def create_trade(
    payload: TradeCreateRequest, user: dict = Depends(auth.get_current_user)
) -> dict:
    return _call_trade_api(
        tr.create_trade,
        user,
        symbol=payload.symbol,
        buy_date=payload.buy_date.isoformat(),
        buy_price=payload.buy_price,
        shares=payload.shares,
    )


@router.post("/api/trades/close")
async def close_trade(
    payload: TradeCloseRequest, user: dict = Depends(auth.get_current_user)
) -> dict:
    return _call_trade_api(
        tr.close_trade,
        user,
        trade_id=payload.trade_id,
        sell_date=payload.sell_date.isoformat(),
        sell_price=payload.sell_price,
    )
