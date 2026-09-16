"""查看路径自愈：切到周/月必须看得到**当期** K 线（及按它算的指标）。

契约（需求方原话）：打开任意标的、切到周/月维度，要看到当前周/月 K 线信息及
其对应指标，不能只看到历史的。

全池任务覆盖不到三类标的 —— 不在标的池（无 metadata 或 enabled=0）、刚加入还没
轮到下一次 16:30、停机/缺席补数久了 —— 它们在页面上原本直接 404。这里锁住
``DataService.ensure_period_history`` 的判定与节流，以及查看接口的接线。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import data.service as data_service
from data.service import DataService, _ONDEMAND_PERIOD_TTL_SECONDS


def _monthly_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": day,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 100.0,
                "amount": 100.0 * price,
            }
            for day, price in rows
        ]
    )


class _StubService(DataService):
    """只替换网络那一步：记录「抓了谁」并把预置数据写回库。"""

    def __init__(self, db, *, frames=None) -> None:
        self.db = db
        self.fetch_calls: list[tuple] = []
        self.frames = frames or {}

    def update_pool_periods(self, symbols, periods=None, **kwargs):
        self.fetch_calls.append((tuple(symbols), tuple(periods or ()), kwargs))
        written = 0
        for symbol in symbols:
            for period in periods or ():
                frame = self.frames.get((period, symbol))
                if frame is None:
                    continue
                written += len(frame)
                for mode in ("raw", "qfq"):
                    self.db.save_market_data(symbol, frame, price_mode=mode, period=period)
        return {
            "status": "completed",
            "periods": {
                period: {"updated": 1 if written else 0, "rows_written": written}
                for period in (periods or ())
            },
        }


@pytest.fixture
def market_db(test_db, monkeypatch):
    """把 get_db 指到隔离库（ensure_period_history 内部默认取 get_db）。"""
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    # 节流表是模块级：用例之间必须隔离，否则后面的用例会被前一个 throttled
    monkeypatch.setattr(data_service, "_ondemand_period_attempts", {})
    return test_db


def _service(market_db, **kwargs) -> _StubService:
    return _StubService(market_db, **kwargs)


class TestEnsurePeriodHistory:
    def test_ready_when_current_period_bar_present(self, market_db) -> None:
        """末根就在当期 → 快路径，零请求。"""
        market_db.save_market_data(
            "READY.SS", _monthly_frame([("2026-08-31", 1.0), ("2026-09-11", 2.0)]),
            price_mode="qfq", period="1M",
        )
        service = _service(market_db)
        result = service.ensure_period_history(
            "READY.SS", "1M", db=market_db, now=date(2026, 9, 12)
        )
        assert result["status"] == "ready"
        assert service.fetch_calls == []

    def test_fetches_when_no_data_at_all(self, market_db) -> None:
        """一只完全没有周期数据的标的（如不在标的池的 551030.SS）→ 抓一次。"""
        service = _service(market_db, frames={("1M", "COLD.SS"): _monthly_frame([("2026-09-11", 3.0)])})
        result = service.ensure_period_history("COLD.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert result["status"] == "fetched"
        assert len(service.fetch_calls) == 1
        symbols, periods, kwargs = service.fetch_calls[0]
        assert symbols == ("COLD.SS",) and periods == ("1M",)
        # 抓完库里就有当期 bar 了（调用方随后重新读库即得）
        stored = market_db.load_market_data("COLD.SS", period="1M")
        assert [str(d)[:10] for d in stored["time"]] == ["2026-09-11"]

    def test_fetches_when_last_bar_is_a_past_period(self, market_db) -> None:
        """有历史但末根停在往期（8 月）→ 也要抓，否则页面只看到历史。"""
        market_db.save_market_data(
            "STALE.SS", _monthly_frame([("2026-07-31", 1.0), ("2026-08-31", 2.0)]),
            price_mode="qfq", period="1M",
        )
        service = _service(market_db)
        result = service.ensure_period_history("STALE.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert result["status"] == "fetched"
        assert len(service.fetch_calls) == 1

    def test_daily_period_is_not_our_business(self, market_db) -> None:
        """日K不走自愈：日 bar 只由 16:30 写入，盘中不见当天是正常的。"""
        service = _service(market_db)
        result = service.ensure_period_history("ANY.SS", "1d", db=market_db)
        assert result["status"] == "not_applicable"
        assert service.fetch_calls == []

    def test_throttled_within_ttl(self, market_db) -> None:
        """确实没有当期 bar 的标的（停牌/退市）不被反复重试。"""
        service = _service(market_db)  # 抓了也没数据 → 下一轮应该被节流
        first = service.ensure_period_history("DEAD.SS", "1M", db=market_db, now=date(2026, 9, 12))
        second = service.ensure_period_history("DEAD.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert first["status"] == "fetched"
        assert second["status"] == "throttled"
        assert len(service.fetch_calls) == 1

    def test_ttl_expiry_allows_retry(self, market_db, monkeypatch) -> None:
        service = _service(market_db)
        service.ensure_period_history("DEAD.SS", "1M", db=market_db, now=date(2026, 9, 12))
        # 模拟时间推进超过 TTL
        monkeypatch.setattr(
            data_service, "_ondemand_period_attempts",
            {k: v - _ONDEMAND_PERIOD_TTL_SECONDS - 1 for k, v in data_service._ondemand_period_attempts.items()},
        )
        again = service.ensure_period_history("DEAD.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert again["status"] == "fetched"
        assert len(service.fetch_calls) == 2

    def test_fetch_failure_reports_without_raising(self, market_db) -> None:
        class _Boom(_StubService):
            def update_pool_periods(self, *a, **kw):
                raise RuntimeError("vendor down")

        result = _Boom(market_db).ensure_period_history("X.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert result["status"] == "failed"
        assert "vendor down" in result["error"]

    def test_weekly_and_monthly_are_independent(self, market_db) -> None:
        """周已是最新、月停在往期时，只补月，不重复抓周。"""
        market_db.save_market_data(
            "MIX.SS", _monthly_frame([("2026-09-11", 1.0)]), price_mode="qfq", period="1w"
        )
        service = _service(market_db)
        weekly = service.ensure_period_history("MIX.SS", "1w", db=market_db, now=date(2026, 9, 12))
        monthly = service.ensure_period_history("MIX.SS", "1M", db=market_db, now=date(2026, 9, 12))
        assert weekly["status"] == "ready"
        assert monthly["status"] == "fetched"
        assert [c[1] for c in service.fetch_calls] == [("1M",)]
