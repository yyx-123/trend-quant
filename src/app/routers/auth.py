"""登录墙认证路由：登录页 / 登录 / 退出。

全站唯一登录入口。登录成功签发 session cookie（HttpOnly + SameSite=Lax），
之后浏览器自动携带，前端无需任何鉴权代码。

安全口径（P1-1/P1-10）：
- 登录接口进程内滑动窗口限流 + 连续失败锁定（services.login_guard），
  登录成败写审计日志（时间/IP/用户名/结果）；
- 退出为 POST（GET 退出可被 CSRF 强制触发），前端经统一 fetch 封装
  携带 X-Requested-With 自定义头调用。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from audit.app_logger import get_logger
from services import auth
from services import trade_records as tr
from services.login_guard import login_guard

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])
from core.paths import web_dir as _web_dir

_templates_dir = _web_dir() / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    # 已登录（含滑动续期内的有效 session）访问登录页直接送回首页。
    # 该路径在登录墙豁免名单里，需自行解析 cookie。
    user, _ = auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(name="login.html", request=request, context={"title": "登录"})


@router.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request) -> JSONResponse:
    ip = _client_ip(request)
    rejected = login_guard.check(ip, payload.username)
    if rejected:
        logger.warning("Login rejected (rate-limit/lock): ip=%s username=%s", ip, payload.username)
        raise HTTPException(status_code=429, detail=rejected)
    try:
        user = tr.authenticate(payload.username, payload.password)
    except tr.TradeAuthError as exc:
        locked = login_guard.record_failure(ip, payload.username)
        logger.warning(
            "Login failed: ip=%s username=%s%s",
            ip, payload.username, " (locked for 10min after this attempt)" if locked else "",
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    login_guard.record_success(ip, payload.username)
    logger.info("Login success: ip=%s username=%s", ip, payload.username)
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


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp
