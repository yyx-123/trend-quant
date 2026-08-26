"""Tests for logging coverage added during the observability pass.

Covers:
- setup_logging: rotating app.log handler + dedicated access.log handler
- rule backtest router: start/completion INFO, swallowed-error WARNING
- global exception handlers: 5xx HTTPException WARNING, unhandled ERROR
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest


def _poll_until_terminal(client, run_id: str, timeout_s: float = 5.0) -> dict:
    from conftest import poll_until_terminal

    return poll_until_terminal(client, f"/rule-backtest/api/progress/{run_id}", timeout_s)




def _remove_route(app, path: str) -> None:
    """卸载测试动态注册的路由，避免污染后续用例。"""
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != path]

class TestSetupLogging:
    def test_rotating_handlers_and_access_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from audit.app_logger import (
            ACCESS_LOG_BACKUP_COUNT,
            APP_LOG_BACKUP_COUNT,
            setup_logging,
        )

        monkeypatch.chdir(tmp_path)
        root = logging.getLogger()
        old_root_handlers = root.handlers[:]
        access_logger = logging.getLogger("uvicorn.access")
        old_access_handlers = access_logger.handlers[:]
        root.handlers.clear()
        access_logger.handlers.clear()
        try:
            setup_logging("INFO")

            app_handlers = [
                h for h in root.handlers
                if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).name == "app.log"
            ]
            assert len(app_handlers) == 1
            assert app_handlers[0].maxBytes > 0
            assert app_handlers[0].backupCount == APP_LOG_BACKUP_COUNT

            access_handlers = [
                h for h in access_logger.handlers
                if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).name == "access.log"
            ]
            assert len(access_handlers) == 1
            assert access_handlers[0].backupCount == ACCESS_LOG_BACKUP_COUNT

            # Idempotent: a second call must not duplicate the access handler.
            setup_logging("INFO")
            assert len([h for h in access_logger.handlers if isinstance(h, RotatingFileHandler)]) == 1
        finally:
            for h in root.handlers + access_logger.handlers:
                h.close()
            root.handlers[:] = old_root_handlers
            access_logger.handlers[:] = old_access_handlers


class TestRuleBacktestLogging:
    def _install_fake_service(self, monkeypatch: pytest.MonkeyPatch, behavior: str) -> None:
        import app.routers.rule_backtest as rb_module

        class FakeRuleBacktestService:
            def run(self, payload: dict, progress_callback=None) -> dict:
                if behavior == "error":
                    raise ValueError("symbol has no market data in range: TEST")
                if progress_callback is not None:
                    progress_callback(1, 1)
                return {"status": "ok", "run_id": "fake", "results": []}

        monkeypatch.setattr(rb_module, "RuleBacktestService", FakeRuleBacktestService)

    def test_start_and_completion_are_logged(self, client, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        self._install_fake_service(monkeypatch, behavior="ok")
        with caplog.at_level(logging.INFO, logger="app.routers.rule_backtest"):
            start = client.post(
                "/rule-backtest/api/run",
                json={"strategy_ids": ["s1"], "symbol": "TEST"},
            )
            final = _poll_until_terminal(client, start.json()["run_id"])
        assert final["status"] == "ok"
        assert "Rule backtest started" in caplog.text
        assert "symbol=TEST" in caplog.text
        assert "Rule backtest completed" in caplog.text

    def test_swallowed_value_error_is_logged_as_warning(
        self, client, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        self._install_fake_service(monkeypatch, behavior="error")
        with caplog.at_level(logging.WARNING, logger="app.routers.rule_backtest"):
            start = client.post(
                "/rule-backtest/api/run",
                json={"strategy_ids": ["s1"], "symbol": "TEST"},
            )
            final = _poll_until_terminal(client, start.json()["run_id"])
        assert final["status"] == "error"
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Rule backtest run_id=" in r.message for r in warning_records)
        assert any("no market data in range" in r.message for r in warning_records)


class TestGlobalExceptionHandlers:
    def test_unhandled_exception_returns_500_and_logs_error(self, client, caplog) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        async def _boom() -> None:
            raise RuntimeError("kaboom")

        app.add_api_route("/_test_observability_boom", _boom)
        # Starlette's ServerErrorMiddleware re-raises after the handler runs;
        # disable client-side re-raise to assert on the actual 500 response.
        # 登录墙后匿名请求会被 303 拦走，local_client 需先登录（tester 由 client fixture 创建）。
        try:
            with (
                caplog.at_level(logging.ERROR, logger="app.main"),
                TestClient(app, raise_server_exceptions=False) as local_client,
            ):
                local_client.post(
                    "/api/auth/login", json={"username": "tester", "password": "pw-tester"}
                )
                resp = local_client.get("/_test_observability_boom")
                assert resp.status_code == 500
                assert resp.json() == {"detail": "Internal Server Error"}
                assert "Unhandled error on GET /_test_observability_boom" in caplog.text
        finally:
            _remove_route(app, "/_test_observability_boom")

    def test_http_exception_5xx_is_logged(self, client, caplog) -> None:
        from fastapi import HTTPException

        from app.main import app

        async def _boom_502() -> None:
            raise HTTPException(status_code=502, detail="upstream broke")

        app.add_api_route("/_test_observability_502", _boom_502)
        try:
            with caplog.at_level(logging.WARNING, logger="app.main"):
                resp = client.get("/_test_observability_502")
            assert resp.status_code == 502
            assert resp.json() == {"detail": "upstream broke"}
            assert "HTTP 502" in caplog.text
        finally:
            _remove_route(app, "/_test_observability_502")

    def test_http_exception_4xx_is_not_logged(self, client, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="app.main"):
            resp = client.get("/definitely-not-a-real-route")
        assert resp.status_code == 404
        assert "HTTP 404" not in caplog.text
