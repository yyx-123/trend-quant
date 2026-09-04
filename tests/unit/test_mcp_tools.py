"""trend_mcp/server.py 薄接口层契约测试：参数透传 + 异常翻译。

server.py 只做「声明工具 → 调 services → 异常翻译成 ok=False」，业务行为的
测试在各 services 模块的测试文件里：
- services.dashboard_snapshot.dashboard_payload（看板路由）→ test_dashboard_snapshot.py
- services.dashboard_snapshot.intraday_dashboard_snapshot → test_dashboard_snapshot.py
- services.dashboard.trend_dashboard_payload / dashboard_lite → test_dashboard_payload.py
- services.symbol_detail.symbol_detail_payload → test_mcp_symbol_detail.py
- services.trade_records.open_positions_overview → test_trade_records_overview.py
- services.instrument_catalog.search_instruments → test_instrument_catalog.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from trend_mcp import server


# ---------------------------------------------------------------------------
# 看板 / 详情 / 标的列表：纯透传
# ---------------------------------------------------------------------------
class TestPassthrough:
    def test_dashboard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake(category="", detail="full", mode="auto"):
            captured.update(category=category, detail=detail, mode=mode)
            return {"ok": True, "data_mode": "intraday_snapshot"}

        monkeypatch.setattr(server, "dashboard_payload", fake)
        result = server.dashboard(category="宽基", detail="lite", mode="intraday")
        assert result["ok"] is True
        assert captured == {"category": "宽基", "detail": "lite", "mode": "intraday"}

    def test_dashboard_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake(category="", detail="full", mode="auto"):
            captured.update(category=category, detail=detail, mode=mode)
            return {"ok": True, "data_mode": "eod"}

        monkeypatch.setattr(server, "dashboard_payload", fake)
        assert server.dashboard()["ok"] is True
        assert captured == {"category": "", "detail": "full", "mode": "auto"}

    def test_symbol_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake(symbol, days=60, rsi_period=14, intraday=False):
            captured.update(symbol=symbol, days=days, rsi_period=rsi_period, intraday=intraday)
            return {"ok": True}

        monkeypatch.setattr(server, "symbol_detail_payload", fake)
        assert server.symbol_detail("510300", days=5, rsi_period=21, intraday=True)["ok"] is True
        assert captured == {"symbol": "510300", "days": 5, "rsi_period": 21, "intraday": True}

    def test_list_instruments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake(category="", keyword="", enabled_only=True):
            captured.update(category=category, keyword=keyword, enabled_only=enabled_only)
            return {"ok": True, "count": 0, "instruments": []}

        monkeypatch.setattr(server, "search_instruments", fake)
        result = server.list_instruments(category="ETF", keyword="300", enabled_only=False)
        assert result["ok"] is True
        assert captured == {"category": "ETF", "keyword": "300", "enabled_only": False}


# ---------------------------------------------------------------------------
# calc_stop_loss
# ---------------------------------------------------------------------------
class TestCalcStopLossContract:
    def test_stop_loss_error_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args, **kwargs):
            raise server.StopLossError("数据不足，无法计算 ATR")

        monkeypatch.setattr(server, "compute_stop_loss", _boom)
        result = server.calc_stop_loss("510300.SS", "2026-08-10", 4.0)
        assert result["ok"] is False
        assert "ATR" in result["error"]

    def test_success_wraps_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server, "compute_stop_loss", lambda *a, **k: {"hard_stop_price": 3.9}
        )
        result = server.calc_stop_loss("510300.SS", "2026-08-10", 4.0)
        assert result == {"ok": True, "hard_stop_price": 3.9}


# ---------------------------------------------------------------------------
# calc_stop_loss_batch
# ---------------------------------------------------------------------------
class TestCalcStopLossBatchContract:
    def test_service_error_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(items, stop_mode=None):
            raise server.StopLossError("items 为空")

        monkeypatch.setattr(server, "compute_stop_loss_batch", _boom)
        assert server.calc_stop_loss_batch([])["ok"] is False

    def test_success_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_batch(items, stop_mode=None):
            captured["stop_mode"] = stop_mode
            captured["n"] = len(items)
            return [
                {"ok": True, "symbol": "510300.SS", "is_intraday": True},
                {"ok": False, "symbol": "BAD.SS", "error": "未找到数据"},
            ]

        monkeypatch.setattr(server, "compute_stop_loss_batch", fake_batch)
        items = [
            {"symbol": "510300.SS", "buy_date": "2026-08-10", "buy_price": 4.0},
            {"symbol": "BAD.SS", "buy_date": "2026-08-10", "buy_price": 1.0},
        ]
        result = server.calc_stop_loss_batch(items, stop_mode="tight")
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["is_intraday"] is True
        assert [r["symbol"] for r in result["results"]] == ["510300.SS", "BAD.SS"]
        assert captured == {"stop_mode": "tight", "n": 2}


# ---------------------------------------------------------------------------
# add_trade / open_positions（通道身份 + 异常翻译）
# ---------------------------------------------------------------------------
class _FakeCtx:
    def __init__(self, scope):
        class RC:
            request = type("R", (), {"scope": scope})()

        self.request_context = RC()


def _ctx_user_scope(username: str | None) -> dict:
    return {"state": {"mcp_user": username} if username else {}}


class TestAddTradeContract:
    def test_token_user_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = server.add_trade(
            "510300.SS", "2026-08-10", 4.0, 100, ctx=_FakeCtx(_ctx_user_scope(None))
        )
        assert result["ok"] is False
        assert "token" in result["error"]

    def test_token_user_missing_in_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(username, db=None):
            raise server.tr.TradeAuthError(
                f"token 映射的用户「{username}」在 users 表中不存在，请检查 TREND_MCP_TOKENS 配置"
            )

        monkeypatch.setattr(server.tr, "user_by_username", _boom)
        result = server.add_trade(
            "510300.SS", "2026-08-10", 4.0, 100, ctx=_FakeCtx(_ctx_user_scope("ghost"))
        )
        assert result["ok"] is False
        assert "users 表中不存在" in result["error"]

    def test_price_out_of_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server.tr,
            "user_by_username",
            lambda username, db=None: {"id": 1, "username": username, "is_admin": True},
        )
        monkeypatch.setattr(
            server.tr,
            "create_trade",
            lambda user, **kw: (_ for _ in ()).throw(server.tr.TradeRecordError("价格超出当日区间")),
        )
        result = server.add_trade(
            "510300.SS", "2026-08-10", 99.0, 100, ctx=_FakeCtx(_ctx_user_scope("yyx"))
        )
        assert result["ok"] is False
        assert "区间" in result["error"]


class TestOpenPositionsContract:
    def test_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_overview(user, stop_mode=None, db=None):
            captured.update(user=user["username"], stop_mode=stop_mode)
            return {"ok": True, "user": user["username"], "positions": []}

        monkeypatch.setattr(
            server, "_token_user", lambda ctx: {"id": 1, "username": "yyx", "is_admin": True}
        )
        monkeypatch.setattr(server.tr, "open_positions_overview", fake_overview)
        result = server.open_positions(stop_mode="tight", ctx=_FakeCtx(_ctx_user_scope("yyx")))
        assert result["ok"] is True
        assert captured == {"user": "yyx", "stop_mode": "tight"}

    def test_auth_error_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(ctx):
            raise server.tr.TradeAuthError("MCP 通道缺少 token 用户映射")

        monkeypatch.setattr(server, "_token_user", _boom)
        result = server.open_positions(ctx=_FakeCtx(_ctx_user_scope(None)))
        assert result["ok"] is False
        assert "token" in result["error"]
