"""main.py 覆盖率补测（目标 ≥95%）：lifespan 内层任务闭包逐一执行验证。"""

from __future__ import annotations

import asyncio

import pytest

from app import main


def _capture_lifespan(monkeypatch: pytest.MonkeyPatch, test_db):
    """跑 lifespan，捕获 scheduler.start 的关键字参数（任务闭包）与线程。"""
    created_threads: list = []
    captured: dict = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None, **kwargs):
            self.target = target
            created_threads.append(self)

        def start(self) -> None:
            pass

    class FakeSchedulerManager:
        def __init__(self, settings=None):
            pass

        def start(self, **kwargs):
            captured.update(kwargs)

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
    return created_threads, captured


class TestBackupJob:
    def test_backup_success(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        assert "backup_job" in jobs
        # 真实执行：临时库 backup_to 成功落盘
        jobs["backup_job"]()
        backups = list((test_db.db_path.parent / "backups").glob("trend_quant-*.db"))
        assert len(backups) == 1  # keep=1

    def test_backup_failure_logged_not_raised(self, test_db, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        import logging

        _, jobs = _capture_lifespan(monkeypatch, test_db)
        monkeypatch.setattr(
            type(test_db), "backup_to",
            lambda self, keep=1: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
        with caplog.at_level(logging.ERROR, logger="app.main"):
            jobs["backup_job"]()  # 异常被吞并记日志，不向外抛
        assert any("Daily DB backup failed" in r.message for r in caplog.records)


class TestUpdateJob:
    def test_lock_prevents_reentry(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        calls: list = []
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: calls.append(force) or {"status": "skipped_non_trading_day"},
        )
        jobs["update_job"](force=True)
        assert calls == [True]
        # 锁已释放后可再次执行
        jobs["update_job"]()
        assert calls == [True, False]

    def test_reentrant_call_skipped(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """持锁期间的嵌套触发被跳过（_update_job_lock 非阻塞防重入）。"""
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        calls: list = []

        def fake_daily_job(settings, force=False):
            calls.append(force)
            if len(calls) == 1:
                jobs["update_job"]()  # 持锁期间嵌套触发 → 应被跳过
            return {"status": "skipped_non_trading_day"}

        monkeypatch.setattr(main, "daily_market_update_job", fake_daily_job)
        jobs["update_job"]()
        # 嵌套调用没有进入 daily_market_update_job 第二次
        assert calls == [False]

    def test_skip_branch_returns_early(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """_run_daily_update：skipped_non_trading_day 直接返回，不跑盘后管线。"""
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        pipeline_calls: list = []
        monkeypatch.setattr(
            main,
            "daily_market_update_job",
            lambda settings, force=False: {"status": "skipped_non_trading_day"},
        )
        monkeypatch.setattr(
            "services.indicator_builder.run_post_update_pipeline",
            lambda *a, **k: pipeline_calls.append(1),
        )
        jobs["update_job"]()
        assert pipeline_calls == []


class TestIntradaySnapshotJob:
    def test_runs_runner(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        seen: list = []

        class FakeRunner:
            def ensure_running(self, trigger: str):
                seen.append(trigger)
                return {"status": "completed"}

        monkeypatch.setattr("services.dashboard_snapshot.snapshot_runner", FakeRunner())
        jobs["intraday_snapshot_job"]()
        assert seen == ["schedule"]

    def test_skipped_status_quiet(self, test_db, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        import logging

        _, jobs = _capture_lifespan(monkeypatch, test_db)
        calls: list = []

        class FakeRunner:
            def ensure_running(self, trigger: str):
                calls.append(trigger)
                return {"status": "skipped"}

        monkeypatch.setattr("services.dashboard_snapshot.snapshot_runner", FakeRunner())
        with caplog.at_level(logging.INFO, logger="app.main"):
            jobs["intraday_snapshot_job"]()
        assert calls == ["schedule"]
        # skipped 不记「scheduled trigger」info 日志
        assert not any("Intraday snapshot scheduled trigger" in r.message for r in caplog.records)


class TestIndustrySyncJob:
    def test_success_records_job(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        _, jobs = _capture_lifespan(monkeypatch, test_db)
        recorded: list = []
        monkeypatch.setattr(
            "services.stock_industry.sync_industry_from_tickflow",
            lambda db=None: {"rows": 10, "written": 8, "reclassify": {"moved": ["A"]}},
        )
        monkeypatch.setattr(
            "services.stock_industry.record_industry_sync_job",
            lambda job_type, summary: recorded.append((job_type, summary)),
        )
        jobs["industry_sync_job"]()
        assert recorded and recorded[0][0] == "stock_industry_sync_tickflow"
        assert recorded[0][1]["rows"] == 10

    def test_failure_logged_not_raised(self, test_db, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        import logging

        _, jobs = _capture_lifespan(monkeypatch, test_db)
        monkeypatch.setattr(
            "services.stock_industry.sync_industry_from_tickflow",
            lambda db=None: (_ for _ in ()).throw(RuntimeError("vendor down")),
        )
        with caplog.at_level(logging.ERROR, logger="app.main"):
            jobs["industry_sync_job"]()  # 失败只记日志
        assert any("Monthly industry sync failed" in r.message for r in caplog.records)


class TestRebuildCheckAndWarm:
    def test_rebuild_check_invokes_warm(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        threads, _ = _capture_lifespan(monkeypatch, test_db)
        rebuild = next(
            (t for t in threads if getattr(t.target, "__name__", "") == "_rebuild_check"), None
        )
        assert rebuild is not None
        calls: list = []
        monkeypatch.setattr(
            "services.indicator_builder.rebuild_if_needed",
            lambda: calls.append("rebuild") or {"status": "ok"},
        )
        monkeypatch.setattr(
            "app.routers.subject_market.warm_dashboard_cache",
            lambda: calls.append("warm"),
        )
        rebuild.target()
        assert calls == ["rebuild", "warm"]

    def test_rebuild_check_failure_still_warms(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        threads, _ = _capture_lifespan(monkeypatch, test_db)
        rebuild = next(
            (t for t in threads if getattr(t.target, "__name__", "") == "_rebuild_check"), None
        )
        calls: list = []
        monkeypatch.setattr(
            "services.indicator_builder.rebuild_if_needed",
            lambda: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
        )
        monkeypatch.setattr(
            "app.routers.subject_market.warm_dashboard_cache",
            lambda: calls.append("warm"),
        )
        rebuild.target()  # 重建失败被吞，仍执行 warm
        assert calls == ["warm"]
