from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import trade_records as tr
from services.manual_trade import compute_manual_trade
from services.stop_loss import StopLossError

router = APIRouter(prefix="/manual-trade", tags=["manual-trade"])
templates = Jinja2Templates(directory="web/templates")


class ManualTradeEvaluateRequest(BaseModel):
    symbol: str = Field(..., min_length=1, description="标的代码，如 510300 或 510300.SS")
    buy_date: date = Field(..., description="买入日期")
    buy_price: float = Field(..., gt=0, description="买入均价")


class Credentials(BaseModel):
    """极简无状态鉴权：每个交易相关请求都携带用户名 + 密码。"""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TradeListRequest(Credentials):
    user_id: int | None = Field(default=None, description="仅 admin 可指定查看的用户")


class TradeCreateRequest(Credentials):
    symbol: str = Field(..., min_length=1)
    buy_date: date
    buy_price: float = Field(..., gt=0)
    shares: float = Field(..., gt=0, description="买入份数")


class TradeCloseRequest(Credentials):
    trade_id: int
    sell_date: date
    sell_price: float = Field(..., gt=0)


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
            payload.symbol, payload.buy_date.isoformat(), payload.buy_price
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


@router.post("/api/users/list")
async def list_users(payload: Credentials) -> list[dict]:
    return _call_trade_api(
        tr.list_users, username=payload.username, password=payload.password
    )


@router.post("/api/trades/list")
async def list_trades(payload: TradeListRequest) -> dict:
    return _call_trade_api(
        tr.list_trades,
        username=payload.username,
        password=payload.password,
        user_id=payload.user_id,
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
