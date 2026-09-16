"""盘中周/月K 当期 bar 的刷新任务（core/jobs.intraday_period_refresh_job）。

口径要点（与 16:30 日更分工）：
- 只在交易时段内跑（``is_realtime_available``：交易日 + 9:30~15:00，含午休）；
  盘外直接跳过，**零请求**；
- 只刷周/月，**日K不动**（日K仍只由 16:30 日更写入收盘数据）；
- 不重复同步除权因子（盘中几乎不变，省掉 ceil(N/50) 次请求），也不记 job_runs
  （一天约 48 轮，记了只是噪声）。
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from core import jobs
from core.bars import PERIOD_MONTHLY, PERIOD_WEEKLY


class _StubService:
    def __init__(self, failed: int = 0) -> None:
        self.calls: list[dict] = []
        self.failed = failed

    def update_pool_periods(self, symbols, periods=None, **kwargs):
        self.calls.append({"symbols": list(symbols), "periods": tuple(periods or ()), **kwargs})
        return {
            "ts": "2026-09-12T10:05:00",
            "status": "completed" if not self.failed else "partial",
            "failed": self.failed,
            "periods": {
                "1w": {"updated": 1, "up_to_date": 0, "failed": 0},
                "1M": {"updated": 1, "up_to_date": 0, "failed": 0},
            },
        }


def _settings():
    return MagicMock(app=MagicMock(update_time_after_close="16:30"))


@pytest.fixture(autouse=True)
def _stub_pool(monkeypatch):
    monkeypatch.setattr(jobs, "_pool_symbols", lambda: ["AAA.SS", "BBB.SS"])


def _freeze_trading(monkeypatch, dt: datetime) -> None:
    monkeypatch.setattr(jobs, "is_realtime_available", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "market_now", lambda: dt)


class TestSessionGate:
    """非交易时段零请求 —— 这是本任务唯一可能打爆 vendor 配额的地方。"""

    @pytest.mark.parametrize(
        "dt,why",
        [
            (datetime(2026, 9, 12, 10, 5), "周六（非交易日）"),
            (datetime(2026, 9, 14, 8, 0), "周一 9:30 前"),
            (datetime(2026, 9, 14, 15, 30), "周一 15:00 后"),
        ],
    )
    def test_outside_session_skips_without_requests(self, monkeypatch, dt, why) -> None:
        monkeypatch.setattr(jobs, "is_realtime_available", lambda *a, **k: False)
        monkeypatch.setattr(jobs, "market_now", lambda: dt)
        service = _StubService()
        result = jobs.intraday_period_refresh_job(_settings(), data_service=service)
        assert result["status"] == "skipped_outside_session", why
        assert service.calls == []

    def test_inside_session_runs(self, monkeypatch) -> None:
        _freeze_trading(monkeypatch, datetime(2026, 9, 14, 10, 5))
        service = _StubService()
        result = jobs.intraday_period_refresh_job(_settings(), data_service=service)
        assert result["status"] == "completed"
        assert len(service.calls) == 1


class TestRefreshScope:
    def test_only_weekly_and_monthly_and_no_factor_sync(self, monkeypatch) -> None:
        """只刷周/月；日K不在此列；不重复同步因子；不写 job_runs。"""
        _freeze_trading(monkeypatch, datetime(2026, 9, 14, 10, 5))
        service = _StubService()
        jobs.intraday_period_refresh_job(_settings(), data_service=service)

        call = service.calls[0]
        assert call["periods"] == (PERIOD_WEEKLY, PERIOD_MONTHLY)
        assert "1d" not in call["periods"]
        assert call["sync_factors"] is False
        assert call["job_type"] == ""  # 不污染 job_runs
        assert call["symbols"] == ["AAA.SS", "BBB.SS"]
        assert call["end_date"] == date(2026, 9, 14)

    def test_summary_omits_per_symbol_detail(self, monkeypatch) -> None:
        """返回值只留汇总：48 轮/天的明细没有消费方。"""
        _freeze_trading(monkeypatch, datetime(2026, 9, 14, 10, 5))
        service = _StubService()
        result = jobs.intraday_period_refresh_job(_settings(), data_service=service)
        assert result["periods"]["1w"] == {"updated": 1, "up_to_date": 0, "failed": 0}
        assert "results" not in result["periods"]["1w"]

    def test_failure_does_not_raise_or_write_sentinel(self, monkeypatch) -> None:
        """盘中失败不自愈报警：下一轮（5 分钟后）会重试，16:30 那轮才是权威。"""
        _freeze_trading(monkeypatch, datetime(2026, 9, 14, 10, 5))
        sentinel = MagicMock()
        monkeypatch.setattr(jobs, "write_sentinel", sentinel)

        class _Boom(_StubService):
            def update_pool_periods(self, *a, **kw):
                raise RuntimeError("vendor down")

        result = jobs.intraday_period_refresh_job(_settings(), data_service=_Boom())
        assert result["status"] == "error"
        assert "vendor down" in result["error"]
        sentinel.assert_not_called()


class TestDailyJobStillSyncsFactors:
    """对照：16:30 那轮必须带因子同步（除权后周/月 qfq 要整段重取）。"""

    def test_daily_period_sync_uses_default_sync_factors(self, monkeypatch, test_db) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        import data.service as data_service

        service = data_service.DataService.__new__(data_service.DataService)
        service.providers = {"tickflow": MagicMock()}
        service.provider_priority = ["tickflow"]
        service.tickflow_settings = None
        seen: dict = {}
        monkeypatch.setattr(
            service, "sync_ex_factors",
            lambda symbols, db=None: (seen.setdefault("called", True), ({}, []))[1],
        )
        monkeypatch.setattr(
            service, "_period_fetch_plan",
            lambda *a, **k: {"symbol": a[0], "status": "up_to_date", "period": a[1]},
        )
        service.update_pool_periods(["AAA.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="")
        assert seen.get("called") is True, "16:30 那轮必须同步因子"

        seen.clear()
        service.update_pool_periods(
            ["AAA.SS"], ("1w",), end_date=date(2026, 9, 12), job_type="", sync_factors=False
        )
        assert seen.get("called") is None, "sync_factors=False 时不应打因子接口"
