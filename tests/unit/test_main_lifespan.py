"""附录 A：app/main.py lifespan 编排（_daily_update_catchup 三路漏更检测）+
AssetVersionMiddleware 行为测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import main


def _run_lifespan_and_capture_threads(monkeypatch: pytest.MonkeyPatch, test_db) -> list:
    """跑一遍 lifespan，捕获其创建的 daemon 线程目标函数。"""
    created: list = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, **kwargs):
            self.target = target
            created.append(self)

        def start(self) -> None:
            pass

    class FakeSchedulerManager:
        def __init__(self, settings=None):
            self.kwargs = None

        def start(self, **kwargs):
            self.kwargs = kwargs

        def shutdown(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setattr(main, "SchedulerManager", FakeSchedulerManager)
    monkeypatch.setattr(main.db_module, "init_db", lambda *a, **k: test_db)
    monkeypatch.delenv("TREND_QUANT_DISABLE_SCHEDULER", raising=False)

    async def _enter():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_enter())
    return created


def _catchup_target(created: list):
    for t in created:
        if t.target is not None and getattr(t.target, "__name__", "") == "_daily_update_catchup":
            return t.target
    raise AssertionError("catchup thread not created")


class TestDailyUpdateCatchup:
    def test_behind_schedule_triggers_force_run(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """最后成功 run_date < 应有交易日且已过 16:30 → 触发一次 force 补跑。"""
        test_db.record_job_run(
            "daily_update", {"ts": "2026-08-20T16:35:00"}, run_date="2026-08-20", status="completed"
        )
        # 2026-08-21 周五 17:00（已过 16:30），expected = 今天
        monkeypatch.setattr(
            "core.calendar.market_now", lambda: datetime(2026, 8, 21, 17, 0).astimezone()
        )
        monkeypatch.setattr("core.calendar.is_trading_day", lambda d: True)
        monkeypatch.setattr(
            "core.calendar.previous_trading_day", lambda d: __import__("datetime").date(2026, 8, 20)
        )

        calls: list[bool] = []
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: calls.append(force) or {"status": "skipped_non_trading_day"},
        )
        created = _run_lifespan_and_capture_threads(monkeypatch, test_db)
        _catchup_target(created)()
        assert calls == [True]

    def test_up_to_date_does_not_trigger(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """今日已成功更新 → 不触发补跑。"""
        test_db.record_job_run(
            "daily_update", {"ts": "2026-08-21T16:35:00"}, run_date="2026-08-21", status="completed"
        )
        monkeypatch.setattr(
            "core.calendar.market_now", lambda: datetime(2026, 8, 21, 17, 0).astimezone()
        )
        monkeypatch.setattr("core.calendar.is_trading_day", lambda d: True)
        monkeypatch.setattr(
            "core.calendar.previous_trading_day", lambda d: __import__("datetime").date(2026, 8, 20)
        )
        # 库内最新K线也是今天（不落后）
        monkeypatch.setattr(
            type(test_db),
            "get_market_dashboard_revision",
            lambda self: ("2026-08-21 00:00:00", "", 1),
        )

        calls: list[bool] = []
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: calls.append(force) or {"status": "skipped_non_trading_day"},
        )
        created = _run_lifespan_and_capture_threads(monkeypatch, test_db)
        _catchup_target(created)()
        assert calls == []

    def test_data_behind_triggers_even_when_job_ok(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """任务「成功」但库内最新K线落后（行情商延迟）→ 仍触发补跑。"""
        test_db.record_job_run(
            "daily_update", {"ts": "2026-08-21T16:35:00"}, run_date="2026-08-21", status="completed"
        )
        monkeypatch.setattr(
            "core.calendar.market_now", lambda: datetime(2026, 8, 21, 17, 0).astimezone()
        )
        monkeypatch.setattr("core.calendar.is_trading_day", lambda d: True)
        monkeypatch.setattr(
            "core.calendar.previous_trading_day", lambda d: __import__("datetime").date(2026, 8, 20)
        )
        monkeypatch.setattr(
            type(test_db),
            "get_market_dashboard_revision",
            lambda self: ("2026-08-20 00:00:00", "", 1),  # max_bar 落后
        )

        calls: list[bool] = []
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: calls.append(force) or {"status": "skipped_non_trading_day"},
        )
        created = _run_lifespan_and_capture_threads(monkeypatch, test_db)
        _catchup_target(created)()
        assert calls == [True]


class TestAssetVersionMiddleware:
    def _make_mw(self):
        async def dummy_app(scope, receive, send):
            pass

        return main.AssetVersionMiddleware(dummy_app)

    def test_http_scope_updates_asset_version(self) -> None:
        mw = self._make_mw()
        app_state = SimpleNamespace(state=SimpleNamespace(asset_version="1"))
        scope = {"type": "http", "app": app_state}
        asyncio.run(mw(scope, None, None))
        # 版本串已按静态目录刷新（启动 sha1 + mtime 格式）
        assert app_state.state.asset_version != ""
        assert "-" in app_state.state.asset_version or len(app_state.state.asset_version) >= 12

    def test_non_http_passthrough(self) -> None:
        mw = self._make_mw()
        scope = {"type": "lifespan"}
        asyncio.run(mw(scope, None, None))
        assert "app" not in scope  # 未触碰 scope


class TestSlowRequestMiddleware:
    def test_slow_request_logged(self, caplog) -> None:
        import logging

        async def slow_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            await asyncio.sleep(0.01)

        mw = main.SlowRequestMiddleware(slow_app)
        mw.SLOW_THRESHOLD_SECONDS = 0.0  # 任何请求都算慢
        scope = {"type": "http", "method": "GET", "path": "/x"}
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        with caplog.at_level(logging.WARNING, logger="app.main"):
            asyncio.run(mw(scope, None, send))
        assert any("Slow request" in r.message for r in caplog.records)

    def test_mcp_path_skipped(self) -> None:
        called = []

        async def app(scope, receive, send):
            called.append(1)

        mw = main.SlowRequestMiddleware(app)
        asyncio.run(mw({"type": "http", "method": "GET", "path": "/mcp/sse"}, None, None))
        assert called == [1]  # 直通，无计时逻辑介入
