"""MCP 通道 Bearer token 鉴权（P0-1/P0-2）。

设计要点（CR 修复方案 §P0-1/P0-2）：
- 纯 ASGI 中间件，包住被挂载的 MCP 子 app（``app.mount("/mcp", McpBearerMiddleware(sse_app))``），
  SSE 的 GET /sse 与 POST /messages/ 均被覆盖。
- token 配置在环境变量 ``TREND_MCP_TOKENS``，格式 ``tokenA=yyx,tokenB=friend1``
  （token → 用户名映射；写工具的用户身份完全来自该映射，不再以工具参数传密码）。
- 校验通过后将用户名写入 ``scope["state"]["mcp_user"]``，工具侧经
  FastMCP ``ctx.request_context.request.scope`` 取回。
- 失败关闭：未配置 token 或 token 缺失/错误一律 401，body 带可读 detail
  （SSE 握手失败是静默的，错误文案必须能排错）。
"""

from __future__ import annotations

import hmac
import json

from audit.app_logger import get_logger
from core.env import mcp_allowed_hosts_raw, mcp_tokens_raw

logger = get_logger(__name__)


def load_mcp_tokens() -> dict[str, str]:
    """解析 TREND_MCP_TOKENS → {token: username}。未配置返回空 dict（失败关闭）。"""
    raw = mcp_tokens_raw()
    tokens: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        token, _, username = part.partition("=")
        token, username = token.strip(), username.strip()
        if token and username:
            tokens[token] = username
    return tokens


def load_mcp_allowed_hosts() -> list[str]:
    """解析 TREND_MCP_ALLOWED_HOSTS（逗号分隔的 frp 域名，可带端口或 :* 通配）。

    非空时才应开启 DNS rebinding 保护；空列表表示保持关闭（上线顺序：
    先验证 Bearer token，再配置域名开启保护）。
    """
    raw = mcp_allowed_hosts_raw()
    return [part.strip() for part in raw.split(",") if part.strip()]


class McpBearerMiddleware:
    """/mcp 前缀的 Bearer token 校验（纯 ASGI，与 AuthWallMiddleware 同风格）。"""

    def __init__(self, app, tokens: dict[str, str]):
        self.app = app
        self.tokens = dict(tokens)

    async def _reject(self, send, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _lookup_token(self, token: str) -> str | None:
        """token → 用户名（常量时间比对，避免时序侧信道）。"""
        for known_token, username in self.tokens.items():
            if hmac.compare_digest(known_token, token):
                return username
        return None

    @staticmethod
    def _bearer_token(scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                text = value.decode("latin-1").strip()
                if text.lower().startswith("bearer "):
                    return text[7:].strip()
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.tokens:
            await self._reject(send, "MCP token not configured on server (TREND_MCP_TOKENS)")
            return
        token = self._bearer_token(scope)
        username = self._lookup_token(token) if token else None
        if username is None:
            await self._reject(send, "missing or invalid MCP token")
            return
        scope.setdefault("state", {})["mcp_user"] = username
        await self.app(scope, receive, send)
