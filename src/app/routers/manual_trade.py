from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import trade_records as tr
from services.manual_trade import compute_manual_trade
from services.stop_loss import StopLossError

router = APIRouter(prefix="/manual-trade", tags=["manual-trade"])
templates = Jinja2Templates(directory="web/templates")

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


class Credentials(BaseModel):
    """极简无状态鉴权：每个交易相关请求都携带用户名 + 密码。"""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TradeCreateRequest(Credentials):
    symbol: str = Field(..., min_length=1)
    buy_date: date
    buy_price: float = Field(..., gt=0)
    shares: float = Field(..., gt=0, description="买入份数")


class TradeCloseRequest(Credentials):
    trade_id: int
    sell_date: date
    sell_price: float = Field(..., gt=0)


class TradeListRequest(Credentials):
    stop_mode: StopMode | None = Field(None, description="止损松紧：tight 紧止损 / loose 松止损")


@router.get("", response_class=HTMLResponse)
async def manual_trade_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="manual_trade.html", request=request, context={"title": "手工交易"}
    )


@router.post("/api/evaluate")
async def evaluate_manual_trade(payload: ManualTradeEvaluateRequest) -> dict:
    """试算（公开，无需登录，不落库）。"""
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


def _call_trade_api(fn, **kwargs):
    """交易记录接口的统一错误映射：401 / 403 / 400。"""
    try:
        return fn(**kwargs)
    except tr.TradeAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except tr.TradePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (tr.TradeRecordError, StopLossError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/login")
async def login(payload: Credentials) -> dict:
    return _call_trade_api(
        tr.authenticate, username=payload.username, password=payload.password
    )


@router.post("/api/trades/list")
async def list_trades(payload: TradeListRequest) -> dict:
    return _call_trade_api(
        tr.list_trades,
        username=payload.username,
        password=payload.password,
        stop_mode=payload.stop_mode,
    )


@router.post("/api/trades/create")
async def create_trade(payload: TradeCreateRequest) -> dict:
    return _call_trade_api(
        tr.create_trade,
        username=payload.username,
        password=payload.password,
        symbol=payload.symbol,
        buy_date=payload.buy_date.isoformat(),
        buy_price=payload.buy_price,
        shares=payload.shares,
    )


@router.post("/api/trades/close")
async def close_trade(payload: TradeCloseRequest) -> dict:
    return _call_trade_api(
        tr.close_trade,
        username=payload.username,
        password=payload.password,
        trade_id=payload.trade_id,
        sell_date=payload.sell_date.isoformat(),
        sell_price=payload.sell_price,
    )
