from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path

from data.storage.db import get_db, init_db, reset_db_instance_for_tests
from services.instrument_jobs import BulkBackfillJobManager, InstrumentAddJobManager


class FakeBackfillService:
    def __init__(self) -> None:
        self.closed = False

    def backfill_daily_history(self, symbol: str, start_date: date, end_date: date, adjust: str) -> dict:
        if symbol == "000003.SZ":
            raise RuntimeError("provider failed")
        if symbol == "000002.SZ":
            return {
                "symbol": symbol,
                "status": "no_data",
                "requested_start": start_date.isoformat(),
                "requested_end": end_date.isoformat(),
                "added_rows": 0,
                "fetched_start": None,
            }
        return {
            "symbol": symbol,
            "status": "updated",
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "added_rows": 2,
            "fetched_start": start_date.isoformat(),
        }

    def backfill_daily_histories(
        self,
        items: list[dict],
        end_date: date,
        adjust: str,
        **kwargs,
    ) -> list[dict]:
        results = []
        for item in items:
            symbol = str(item.get("symbol") or "")
            try:
                result = self.backfill_daily_history(symbol, item["start_date"], end_date, adjust)
                results.append({"ok": True, "result": result})
            except Exception as exc:
                results.append({"ok": False, "symbol": symbol, "error": str(exc)})
        return results

    def close(self) -> None:
        self.closed = True


class BlockingBackfillService:
    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def backfill_daily_history(self, symbol: str, start_date: date, end_date: date, adjust: str) -> dict:
        self.release.wait(timeout=2)
        return {
            "symbol": symbol,
            "status": "updated",
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "added_rows": 1,
            "fetched_start": start_date.isoformat(),
        }

    def backfill_daily_histories(
        self,
        items: list[dict],
        end_date: date,
        adjust: str,
        **kwargs,
    ) -> list[dict]:
        return [
            {
                "ok": True,
                "result": self.backfill_daily_history(
                    str(item.get("symbol") or ""),
                    item["start_date"],
                    end_date,
                    adjust,
                ),
            }
            for item in items
        ]

    def close(self) -> None:
        pass


class AllFailedBackfillService:
    def backfill_daily_histories(
        self,
        items: list[dict],
        end_date: date,
        adjust: str,
        **kwargs,
    ) -> list[dict]:
        return [
            {
                "ok": False,
                "symbol": str(item.get("symbol") or ""),
                "error": "无日/周/月K线查询批量查询权限",
            }
            for item in items
        ]

    def close(self) -> None:
        pass


def wait_for_terminal(manager: BulkBackfillJobManager, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = manager.snapshot()
        if status["status"] in {"completed", "failed"}:
            return status
        time.sleep(0.02)
    raise AssertionError("bulk backfill job did not finish")


class BulkBackfillJobManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        init_db(self.tmp_path / "test.db")

    def tearDown(self) -> None:
        # Worker threads may still be flushing the final job_runs write
        # (WAL files) right after the terminal status is published.
        time.sleep(0.3)
        reset_db_instance_for_tests()
        self.tmp.cleanup()

    def test_runs_in_background_and_summarizes_results(self) -> None:
        services: list[FakeBackfillService] = []

        def factory(provider_priority: list[str] | None) -> FakeBackfillService:
            service = FakeBackfillService()
            services.append(service)
            return service

        manager = BulkBackfillJobManager(data_service_factory=factory)
        started, status = manager.start(
            items=[
                {"symbol": "000001.SZ", "start_date": date(2026, 7, 1)},
                {"symbol": "000002.SZ", "start_date": date(2026, 7, 1)},
                {"symbol": "000003.SZ", "start_date": date(2026, 7, 1)},
            ],
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=["tickflow"],
        )

        self.assertTrue(started)
        self.assertEqual(status["status"], "running")

        done = wait_for_terminal(manager)

        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress_current"], 3)
        self.assertEqual(done["progress_total"], 3)
        self.assertEqual(done["summary"]["updated"], 1)
        self.assertEqual(done["summary"]["no_data"], 1)
        self.assertEqual(done["summary"]["failed"], 1)
        self.assertEqual(done["summary"]["added_rows"], 2)
        # P2-11 单例化后任务结束不再 close() service（共享实例语义）——
        # factory 返回的实例应保持未关闭状态。
        deadline = time.time() + 0.2
        while time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(services[0].closed)
        # P2-9：start 落 running 行 + 完成落终态行，共 2 条
        runs = get_db().list_job_runs("instrument_bulk_backfill")
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["payload"]["status"], "completed")
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[1]["status"], "running")

    def test_rejects_second_job_while_running(self) -> None:
        release = threading.Event()
        manager = BulkBackfillJobManager(
            data_service_factory=lambda provider_priority: BlockingBackfillService(release),
        )

        started, status = manager.start(
            items=[{"symbol": "000001.SZ", "start_date": date(2026, 7, 1)}],
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )
        self.assertTrue(started)
        self.assertEqual(status["status"], "running")

        started_again, second_status = manager.start(
            items=[{"symbol": "000002.SZ", "start_date": date(2026, 7, 1)}],
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )
        self.assertFalse(started_again)
        self.assertEqual(second_status["job_id"], status["job_id"])

        release.set()
        self.assertEqual(wait_for_terminal(manager)["status"], "completed")

    def test_marks_job_failed_when_every_symbol_fails(self) -> None:
        manager = BulkBackfillJobManager(
            data_service_factory=lambda provider_priority: AllFailedBackfillService(),
        )

        started, status = manager.start(
            items=[{"symbol": "000001.SZ", "start_date": date(2026, 7, 1)}],
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )

        self.assertTrue(started)
        self.assertEqual(status["status"], "running")

        done = wait_for_terminal(manager)

        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["summary"]["failed"], 1)
        self.assertIn("批量查询权限", done["error"])


class InstrumentAddJobManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        init_db(self.tmp_path / "test.db")
        get_db().save_instrument_categories(
            [
                {"path": "股票", "level": 1, "name": "股票", "priority": 3},
                {
                    "path": "股票-创业板",
                    "level": 2,
                    "name": "创业板",
                    "parent_path": "股票",
                    "priority": 1,
                },
                {
                    "path": "股票-创业板-电源设备",
                    "level": 3,
                    "name": "电源设备",
                    "parent_path": "股票-创业板",
                    "priority": 1,
                },
                {"path": "商品", "level": 1, "name": "商品", "priority": 4},
                {
                    "path": "商品-贵金属",
                    "level": 2,
                    "name": "贵金属",
                    "parent_path": "商品",
                    "priority": 1,
                },
                {
                    "path": "商品-贵金属-实物黄金",
                    "level": 3,
                    "name": "实物黄金",
                    "parent_path": "商品-贵金属",
                    "priority": 1,
                },
            ]
        )

    def tearDown(self) -> None:
        reset_db_instance_for_tests()
        self.tmp.cleanup()

    def test_add_job_writes_config_metadata_and_backfills(self) -> None:
        manager = InstrumentAddJobManager(
            data_service_factory=lambda provider_priority: FakeBackfillService(),
        )

        started, status = manager.start(
            item={
                "symbol": "301516.SZ",
                "name": "中远通",
                "category_l1": "股票",
                "category_l2": "创业板",
                "category_l3": "电源设备",
            },
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )

        self.assertTrue(started)
        self.assertEqual(status["status"], "running")
        done = wait_for_terminal(manager)

        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress_current"], 3)
        self.assertEqual(done["summary"]["config_saved"], 1)
        self.assertEqual(done["summary"]["metadata_saved"], 1)
        self.assertEqual(done["summary"]["added_rows"], 2)
        saved = get_db().get_instrument_metadata("301516.SZ")
        self.assertEqual(saved["asset_type"], "stock")
        self.assertEqual(saved["category_path"], "股票-创业板-电源设备")
        self.assertEqual(saved["enabled"], 1)
        self.assertEqual(saved["stop_atr_mul"], 1.5)

    def test_rejects_second_add_job_while_running(self) -> None:
        release = threading.Event()
        manager = InstrumentAddJobManager(
            data_service_factory=lambda provider_priority: BlockingBackfillService(release),
        )

        started, status = manager.start(
            item={
                "symbol": "518850.SS",
                "name": "黄金ETF华夏",
                "category_l1": "商品",
                "category_l2": "贵金属",
                "category_l3": "实物黄金",
            },
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )
        self.assertTrue(started)

        started_again, second_status = manager.start(
            item={
                "symbol": "159985.SZ",
                "name": "豆粕ETF华夏",
                "category_l1": "商品",
                "category_l2": "贵金属",
                "category_l3": "实物黄金",
            },
            end_date=date(2026, 7, 7),
            adjust="qfq",
            provider_priority=None,
        )
        self.assertFalse(started_again)
        self.assertEqual(second_status["job_id"], status["job_id"])

        release.set()
        self.assertEqual(wait_for_terminal(manager)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
