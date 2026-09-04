"""P0-1/P0-2：MCP 通道 Bearer token 鉴权与工具去密码化测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from app.mcp_auth import McpBearerMiddleware, load_mcp_allowed_hosts, load_mcp_tokens
from services import trade_records as tr
from trend_mcp import server


# ---------------------------------------------------------------------------
# token / allowed_hosts 配置解析
# ---------------------------------------------------------------------------
class TestLoadMcpTokens:
    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("TREND_MCP_TOKENS", raising=False)
        assert load_mcp_tokens() == {}

    def test_parses_token_user_pairs(self, monkeypatch):
        monkeypatch.setenv("TREND_MCP_TOKENS", " tokenA=yyx , tokenB=friend1 ")
        assert load_mcp_tokens() == {"tokenA": "yyx", "tokenB": "friend1"}

    def test_skips_malformed_parts(self, monkeypatch):
        monkeypatch.setenv("TREND_MCP_TOKENS", "tokenA=yyx,,nokey,=nouser,notoken=")
        assert load_mcp_tokens() == {"tokenA": "yyx"}


class TestLoadMcpAllowedHosts:
    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("TREND_MCP_ALLOWED_HOSTS", raising=False)
        assert load_mcp_allowed_hosts() == []

    def test_parses_hosts(self, monkeypatch):
        monkeypatch.setenv("TREND_MCP_ALLOWED_HOSTS", "mcp.example.com, mcp.example.com:*")
        assert load_mcp_allowed_hosts() == ["mcp.example.com", "mcp.example.com:*"]


# ---------------------------------------------------------------------------
# McpBearerMiddleware（纯 ASGI 行为）
# ---------------------------------------------------------------------------
def _run_asgi(app, scope):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/mcp/sse",
        "headers": headers or [],
    }


class _EchoApp:
    """记录 scope 并返回 200 的最小 ASGI app。"""

    def __init__(self):
        self.seen_scope = None

    async def __call__(self, scope, receive, send):
        self.seen_scope = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class TestMcpBearerMiddleware:
    def test_no_tokens_configured_fails_closed(self):
        sent = _run_asgi(McpBearerMiddleware(_EchoApp(), {}), _http_scope())
        assert sent[0]["status"] == 401
        body = json.loads(sent[1]["body"])
        assert "not configured" in body["detail"]

    def test_missing_token_rejected(self):
        sent = _run_asgi(McpBearerMiddleware(_EchoApp(), {"t1": "yyx"}), _http_scope())
        assert sent[0]["status"] == 401
        assert json.loads(sent[1]["body"])["detail"] == "missing or invalid MCP token"

    def test_wrong_token_rejected(self):
        headers = [(b"authorization", b"Bearer wrong")]
        sent = _run_asgi(McpBearerMiddleware(_EchoApp(), {"t1": "yyx"}), _http_scope(headers))
        assert sent[0]["status"] == 401

    def test_valid_token_passes_and_sets_user(self):
        echo = _EchoApp()
        headers = [(b"Authorization", b"Bearer t1")]
        sent = _run_asgi(McpBearerMiddleware(echo, {"t1": "yyx"}), _http_scope(headers))
        assert sent[0]["status"] == 200
        assert echo.seen_scope["state"]["mcp_user"] == "yyx"

    def test_non_http_scope_passes_through(self):
        echo = _EchoApp()
        sent = _run_asgi(McpBearerMiddleware(echo, {}), {"type": "lifespan"})
        assert sent[0]["status"] == 200


# ---------------------------------------------------------------------------
# _token_user：工具侧身份解析
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, scope):
        self.scope = scope


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _FakeCtx:
    def __init__(self, scope):
        self.request_context = _FakeRequestContext(_FakeRequest(scope))


class TestTokenUser:
    def test_no_request_context_raises(self):
        with pytest.raises(tr.TradeAuthError, match="缺少 token 用户映射"):
            server._token_user(object())

    def test_no_mcp_user_in_scope_raises(self):
        with pytest.raises(tr.TradeAuthError, match="缺少 token 用户映射"):
            server._token_user(_FakeCtx({"state": {}}))

    def test_mapped_user_missing_in_db_raises(self, monkeypatch):
        class FakeDb:
            def get_user_by_username(self, username):
                return None

        monkeypatch.setattr(tr, "get_db", lambda: FakeDb())
        with pytest.raises(tr.TradeAuthError, match="users 表中不存在"):
            server._token_user(_FakeCtx({"state": {"mcp_user": "ghost"}}))

    def test_valid_mapping_returns_user(self, monkeypatch):
        class FakeDb:
            def get_user_by_username(self, username):
                assert username == "yyx"
                return {"id": 7, "username": "yyx", "is_admin": True}

        monkeypatch.setattr(tr, "get_db", lambda: FakeDb())
        user = server._token_user(_FakeCtx({"state": {"mcp_user": "yyx"}}))
        assert user == {"id": 7, "username": "yyx", "is_admin": True}


# ---------------------------------------------------------------------------
# 工具 schema：username/password/ctx 均不应对外暴露
# ---------------------------------------------------------------------------
class TestToolSchema:
    def test_write_tools_have_no_credential_params(self):
        tools = server.mcp._tool_manager._tools
        for name in ("add_trade", "open_positions"):
            params = tools[name].parameters["properties"]
            assert "username" not in params
            assert "password" not in params
            assert "ctx" not in params

    def test_add_trade_keeps_business_params(self):
        params = server.mcp._tool_manager._tools["add_trade"].parameters["properties"]
        assert set(params) == {"symbol", "buy_date", "buy_price", "shares"}
