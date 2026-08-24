"""登录墙认证路由：登录页 / 登录 / 退出 / 当前用户。

全站唯一登录入口。登录成功签发 session cookie（HttpOnly + SameSite=Lax），
之后浏览器自动携带，前端无需任何鉴权代码。退出为 GET 链接（导航栏直接
引用），清 session + cookie 后跳回登录页。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from services import auth
from services import trade_records as tr

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="web/templates")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    # 已登录（含滑动续期内的有效 session）访问登录页直接送回首页。
    # 该路径在登录墙豁免名单里，需自行解析 cookie。
    user, _ = auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(name="login.html", request=request, context={"title": "登录"})


@router.post("/api/auth/login")
async def login(payload: LoginRequest) -> JSONResponse:
    try:
        user = tr.authenticate(payload.username, payload.password)
    except tr.TradeAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token = auth.issue_session(user["id"])
    resp = JSONResponse(user)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
    )
    return resp


@router.get("/api/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@router.get("/api/auth/me")
async def me(user: dict = Depends(auth.get_current_user)) -> dict:
    return user
