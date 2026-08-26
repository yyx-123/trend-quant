"""登录墙会话服务 — cookie session 的签发 / 校验 / 滑动续期 / 销毁。

机制：登录成功后服务端生成随机 token 写入 sessions 表，并以 HttpOnly
cookie（``SESSION_COOKIE``）下发；之后浏览器对全站每个页面 / API 请求自动
携带该 cookie，登录墙中间件（app.main.AuthWallMiddleware）据此识别用户。

过期策略为滑动续期：剩余有效期不足一半时自动顺延 ``SESSION_TTL`` 并重新
下发 cookie，活跃用户实际永不过期；连续 ``SESSION_TTL`` 未访问才需重新登录。

MCP 工具（trend_mcp）不走 cookie：通道级 Bearer token 鉴权（``app/mcp_auth.py``），
token→用户映射后由工具侧换取 user，与本模块互不干扰。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from audit.app_logger import get_logger
from data.storage.db import get_db

logger = get_logger(__name__)

SESSION_COOKIE = "tq_session"
SESSION_TTL = timedelta(days=30)
# 剩余有效期低于该阈值才续期（避免每个请求都写库 + 重发 cookie）
_RENEW_THRESHOLD = SESSION_TTL / 2

__all__ = [
    "SESSION_COOKIE",
    "SESSION_TTL",
    "destroy_session",
    "get_current_user",
    "issue_session",
    "resolve_session",
]


def issue_session(user_id: int, db=None) -> str:
    """为已验证用户签发 session，返回 token。顺手清理全库过期 session。"""
    db = db or get_db()
    db.delete_expired_sessions(datetime.now())
    token = secrets.token_hex(32)
    db.create_session(int(user_id), token, datetime.now() + SESSION_TTL)
    return token


def resolve_session(token: str | None, db=None) -> tuple[dict | None, bool]:
    """校验 session token。

    返回 ``(user, renewed)``：user 为 ``{id, username, is_admin}`` 或 None；
    renewed=True 表示本次触发了滑动续期，调用方应在响应里重新下发 cookie
    （有效期已与服务端同步顺延）。
    """
    if not token:
        return None, False
    db = db or get_db()
    row = db.get_session_user(token)
    if row is None:
        return None, False
    now = datetime.now()
    try:
        expires_at = datetime.strptime(str(row["session_expires_at"]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        expires_at = now - timedelta(seconds=1)
    if expires_at <= now:
        db.delete_session(token)
        return None, False
    user = row["user"]
    renewed = False
    if expires_at - now < _RENEW_THRESHOLD:
        db.touch_session(token, now + SESSION_TTL)
        renewed = True
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}, renewed


def destroy_session(token: str | None, db=None) -> None:
    if not token:
        return
    db = db or get_db()
    db.delete_session(token)


def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：从 request.state 取登录墙中间件已解析的用户。

    中间件保证到达业务路由的请求必有有效 session，这里仅做兜底 401
    （豁免路径内的接口若挂此依赖而中间件未解析，也会正确拦截）。
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user
