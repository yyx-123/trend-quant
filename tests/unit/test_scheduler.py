"""附录 A：core/scheduler.py 任务注册/幂等/时刻表 + RevisionCache single-flight。"""

from __future__ import annotations

import threading
import time

from core.scheduler import INTRADAY_SNAPSHOT_CRONS, SchedulerManager
from services.dashboard import RevisionCache


def _settings():
    from core.settings import load_settings

    return load_settings()


class TestSchedulerRegistration:
    def test_jobs_registered_with_expected_ids_and_misfire(self) -> None:
        mgr = SchedulerManager(settings=_settings())
        mgr.start(
            update_job=lambda: None,
            intraday_snapshot_job=lambda: None,
            industry_sync_job=lambda: None,
            backup_job=lambda: None,
        )
        try:
            jobs = {j["id"] for j in mgr.jobs_snapshot()}
            assert "daily_update" in jobs
            assert "daily_db_backup" in jobs
            assert "stock_industry_sync" in jobs
            assert {f"intraday_snapshot_{i}" for i in range(len(INTRADAY_SNAPSHOT_CRONS))} <= jobs

            by_id = {j.id: j for j in mgr.scheduler.get_jobs()}
            assert by_id["daily_update"].misfire_grace_time == 7200
            assert by_id["daily_db_backup"].misfire_grace_time == 7200
            assert by_id["stock_industry_sync"].misfire_grace_time == 86400
        finally:
            mgr.shutdown()

    def test_start_is_idempotent(self) -> None:
        mgr = SchedulerManager(settings=_settings())
        mgr.start(update_job=lambda: None)
        first = mgr.scheduler
        mgr.start(update_job=lambda: None)  # 二次 start 直接返回
        assert mgr.scheduler is first
        mgr.shutdown()
        assert mgr.jobs_snapshot() == []

    def test_shutdown_clears(self) -> None:
        mgr = SchedulerManager(settings=_settings())
        mgr.start(update_job=lambda: None)
        mgr.shutdown()
        assert mgr.scheduler is None
        assert mgr.jobs_snapshot() == []


class TestIntradaySnapshotCrons:
    def test_all_slots_within_sessions(self) -> None:
        """cron 展开的时刻全部落在 9:35-11:30 / 13:00-15:00。"""
        for hour_expr, minute_expr in INTRADAY_SNAPSHOT_CRONS:
            hours = self._expand(hour_expr, 0, 23)
            minutes = self._expand(minute_expr, 0, 59)
            assert hours and minutes
            for h in hours:
                for m in minutes:
                    t = h * 60 + m
                    in_morning = 9 * 60 + 35 <= t <= 11 * 60 + 30
                    in_afternoon = 13 * 60 <= t <= 15 * 60
                    assert in_morning or in_afternoon, f"{hour_expr}/{minute_expr} → {h:02d}:{m:02d}"

    @staticmethod
    def _expand(expr: str, lo: int, hi: int) -> list[int]:
        out: set[int] = set()
        for part in expr.split(","):
            if part == "*":
                out.update(range(lo, hi + 1))
                continue
            step = 1
            if "/" in part:
                part, step_s = part.split("/")
                step = int(step_s)
            if part == "*":
                out.update(range(lo, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-")
                out.update(range(int(a), int(b) + 1, step))
            else:
                out.add(int(part))
        return sorted(v for v in out if lo <= v <= hi)


class TestRevisionCache:
    def test_same_revision_computes_once(self) -> None:
        cache = RevisionCache()
        calls = []
        first = cache.get_or_compute(("rev1",), lambda: calls.append(1) or {"v": 1})
        second = cache.get_or_compute(("rev1",), lambda: calls.append(2) or {"v": 2})
        assert first is second
        assert calls == [1]

    def test_revision_change_recomputes(self) -> None:
        cache = RevisionCache()
        cache.get_or_compute(("rev1",), lambda: {"v": 1})
        third = cache.get_or_compute(("rev2",), lambda: {"v": 2})
        assert third == {"v": 2}

    def test_concurrent_single_flight(self) -> None:
        """并发冷启动：只有一个线程真正计算，其余等待后读同一份结果。"""
        cache = RevisionCache()
        compute_calls: list[str] = []
        gate = threading.Event()

        def compute() -> dict:
            compute_calls.append("x")
            gate.wait(timeout=5)
            return {"v": 1}

        results: list[dict] = []
        threads = [threading.Thread(target=lambda: results.append(cache.get_or_compute(("r",), compute))) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in threads:
            t.join(timeout=5)
        assert len(compute_calls) == 1
        assert len(results) == 8
        assert all(r is results[0] for r in results)
