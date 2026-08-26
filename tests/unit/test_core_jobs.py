"""附录 A：core/jobs.py（每日 16:30 补库主任务）行为契约测试。

覆盖：非交易日跳过（落 job_runs）、失败落 job_runs 并 re-raise、
_pool_symbols 去重/剔除 disabled/兜底 benchmark。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core import jobs
from core.jobs import _pool_symbols, daily_market_update_job


def _settings():
    from core.settings import load_settings

    return load_settings()


class TestNonTradingDaySkip:
    def test_skips_and_records_job_run(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        # 2026-08-22 是周六
        monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 8, 22, 16, 30))
        monkeypatch.setattr(jobs, "is_trading_day", lambda d: False)

        called = []
        monkeypatch.setattr(
            jobs.DataService, "update_pool_daily", lambda self, **kw: called.append(kw) or {}
        )

        payload = daily_market_update_job(_settings())
        assert payload["status"] == "skipped_non_trading_day"
        assert called == []
        run = test_db.get_latest_job_run("daily_update_skip")
        assert run is not None
        assert run["status"] == "skipped_non_trading_day"
        assert run["run_date"] == "2026-08-22"

    def test_force_runs_on_non_trading_day(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        """force=True（启动补偿）在非交易日也执行。"""
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 8, 22, 10, 0))
        monkeypatch.setattr(jobs, "is_trading_day", lambda d: False)

        class FakeService:
            def update_pool_daily(self, **kw):
                return {"total": 0, "success": 0, "failed": 0, "results": []}

        monkeypatch.setattr(jobs, "get_data_service", lambda: FakeService())
        payload = daily_market_update_job(_settings(), force=True)
        assert payload.get("status") != "skipped_non_trading_day"
        assert payload.get("total") == 0
        # 空标的池时兜底为 benchmark 清单（中证500/创业板）
        assert "510500.SS" in payload["symbols"]
        assert "159915.SZ" in payload["symbols"]


class TestFailureRecording:
    def test_failure_recorded_and_reraised(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 8, 21, 16, 30))
        monkeypatch.setattr(jobs, "is_trading_day", lambda d: True)

        class BoomService:
            def update_pool_daily(self, **kw):
                raise RuntimeError("vendor exploded")

        monkeypatch.setattr(jobs, "get_data_service", lambda: BoomService())

        with pytest.raises(RuntimeError, match="vendor exploded"):
            daily_market_update_job(_settings())

        run = test_db.get_latest_job_run("daily_update")
        assert run is not None
        assert run["status"] == "failed"
        assert "vendor exploded" in str(run["payload"])


class TestPoolSymbols:
    def test_disabled_excluded_dedup_upper(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        test_db.save_instrument_metadata([
            {"symbol": "510300.SS", "name": "A", "category_l1": "a", "category_l2": "b",
             "category_l3": "c", "enabled": True},
            {"symbol": "588000.SS", "name": "B", "category_l1": "a", "category_l2": "b",
             "category_l3": "c", "enabled": False},  # disabled 剔除
            {"symbol": "510300.SS", "name": "A2", "category_l1": "a", "category_l2": "b",
             "category_l3": "c", "enabled": True},  # 重复去重
        ])
        monkeypatch.setattr(db_module, "get_db", lambda: test_db)

        symbols = _pool_symbols()
        assert symbols.count("510300.SS") == 1
        assert "588000.SS" not in symbols
        # benchmark 兜底在内（中证500/创业板）
        assert "510500.SS" in symbols
        assert symbols == [s.upper() for s in symbols]

    def test_db_failure_falls_back_to_benchmarks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(db_module, "get_db", _boom)
        symbols = _pool_symbols()
        assert symbols == ["510500.SS", "159915.SZ"]


class TestSentinel:
    def test_failure_writes_sentinel(self, test_db, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """P2-22：任务失败写哨兵文件，成功清除。"""
        import core.ops_sentinel as sentinel
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)
        monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 8, 21, 16, 30))
        monkeypatch.setattr(jobs, "is_trading_day", lambda d: True)
        monkeypatch.setattr(sentinel, "_RUNTIME_DIR", tmp_path)

        class BoomService:
            def update_pool_daily(self, **kw):
                raise RuntimeError("vendor exploded")

        monkeypatch.setattr(jobs, "get_data_service", lambda: BoomService())
        with pytest.raises(RuntimeError):
            daily_market_update_job(_settings())
        assert (tmp_path / "daily_update.failed.json").exists()

        class OkService:
            def update_pool_daily(self, **kw):
                return {"total": 1, "success": 1, "failed": 0, "results": []}

        monkeypatch.setattr(jobs, "get_data_service", lambda: OkService())
        daily_market_update_job(_settings())
        assert not (tmp_path / "daily_update.failed.json").exists()
