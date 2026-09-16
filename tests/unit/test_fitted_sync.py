"""拟合周/月K 的 service 维护路径。

停写保表（2026-09-16）：表自上线起零生产消费方，日更钩子已摘除
（ensure_daily_history 不再调用 refresh_fitted_period_bars），方法与
历史数据保留供手工回填/未来回测 PIT 场景。本文件钉死两件事：

- refresh_fitted_period_bars 方法本身仍可用（full/since/空表语义）；
- ensure_daily_history 不再触发任何 fitted 写入。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

import data.service as data_service
from data.storage.market_store import MarketStore


def _daily(rows: list[tuple]) -> pd.DataFrame:
    # time 统一用 Timestamp（落库为 'YYYY-MM-DD HH:MM:SS'）：与 vendor 帧格式一致，
    # 混用 'YYYY-MM-DD' 字符串会让同一日期产生两个主键（upsert 失效的坑）。
    return pd.DataFrame(
        [
            {"time": pd.Timestamp(d), "open": p, "high": p + 0.1, "low": p - 0.1, "close": p,
             "volume": v, "amount": v * p}
            for d, p, v in rows
        ]
    )


class _FakeProvider:
    def __init__(self, frames: dict | None = None):
        self.frames = frames or {}

    def fetch_daily_history(self, symbol, start, end, adjust="none"):
        return self.frames.get((symbol, adjust), pd.DataFrame())

    def close(self) -> None:  # pragma: no cover - 接口占位
        pass


def _make_service(monkeypatch, provider, test_db) -> data_service.DataService:
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    service = data_service.DataService.__new__(data_service.DataService)
    service.providers = {"tickflow": provider}
    service.provider_priority = ["tickflow"]
    service.tickflow_settings = None
    service.market_store = MarketStore()
    service.raw_store = MarketStore(price_mode="raw")
    return service


def _seed_symbol(service, test_db, symbol: str, daily: pd.DataFrame) -> None:
    """把 raw/qfq 双侧都种下日K，并建好拟合表初始状态。"""
    service.raw_store.save_history(symbol, daily)
    test_db.save_market_data(symbol, daily, price_mode="qfq")


class TestRefreshFitted:
    def test_full_rebuild(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _daily(
            [("2026-09-07", 10.0, 100.0), ("2026-09-08", 11.0, 200.0),
             ("2026-09-14", 12.0, 300.0)]
        )
        _seed_symbol(service, test_db, "AAA.SS", daily)
        out = service.refresh_fitted_period_bars("AAA.SS", full=True)
        assert out == {"symbol": "AAA.SS", "1w": 3, "1M": 3}
        weekly = test_db.load_fitted_period_bars("AAA.SS", "1w")
        assert len(weekly) == 3
        assert weekly.iloc[1]["volume"] == 300.0  # 当周累计
        monthly = test_db.load_fitted_period_bars("AAA.SS", "1M")
        assert monthly.iloc[2]["volume"] == 600.0  # 当月累计

    def test_since_only_touches_affected_period_rows(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        daily = _daily(
            [("2026-09-07", 10.0, 100.0), ("2026-09-08", 11.0, 200.0)]
        )
        _seed_symbol(service, test_db, "AAA.SS", daily)
        service.refresh_fitted_period_bars("AAA.SS", full=True)
        # 新的一天到来：qfq 追加 09-09，按 since=09-09 增量维护
        new_daily = _daily(
            [("2026-09-07", 10.0, 100.0), ("2026-09-08", 11.0, 200.0),
             ("2026-09-09", 12.0, 400.0)]
        )
        test_db.save_market_data("AAA.SS", new_daily, price_mode="qfq")
        out = service.refresh_fitted_period_bars("AAA.SS", since=date(2026, 9, 9))
        # 只重建受影响周期（当周/当月）的行：3 行；既有行内容不变（幂等重写）
        assert out["1w"] == 3 and out["1M"] == 3
        weekly = test_db.load_fitted_period_bars("AAA.SS", "1w")
        assert len(weekly) == 3
        assert weekly.iloc[2]["volume"] == 700.0  # 新行累计了整周
        assert weekly.iloc[1]["volume"] == 300.0  # 既有行值不变
        # 上一周期（若有）不受影响：这里没有更早的行，总行数即当周的 3 行

    def test_empty_qfq_is_noop(self, monkeypatch, test_db) -> None:
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        out = service.refresh_fitted_period_bars("NOPE.SS", full=True)
        assert out == {"symbol": "NOPE.SS", "1w": 0, "1M": 0}


class TestDailyUpdateHookRemoved:
    """停写保表：日更不再维护 fitted 表（2026-09-16 摘除钩子）。"""

    def test_raw_update_does_not_write_fitted(self, monkeypatch, test_db) -> None:
        # 存量日K 到 09-10；provider 给出 09-11 的新 bar
        old_daily = _daily(
            [("2026-09-07", 10.0, 100.0), ("2026-09-10", 11.0, 200.0)]
        )
        provider = _FakeProvider(
            {("AAA.SS", "none"): _daily([("2026-09-11", 12.0, 300.0)])}
        )
        service = _make_service(monkeypatch, provider, test_db)
        _seed_symbol(service, test_db, "AAA.SS", old_daily)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, db=None: ({}, []))
        monkeypatch.setattr(service, "refresh_fitted_period_bars", MagicMock())

        result = service.ensure_daily_history("AAA.SS", date(2026, 1, 1), date(2026, 9, 11))

        assert result["status"] == "updated"  # 日更本身照常
        service.refresh_fitted_period_bars.assert_not_called()
        assert test_db.load_fitted_period_bars("AAA.SS", "1w").empty

    def test_factor_change_does_not_rebuild_fitted(self, monkeypatch, test_db) -> None:
        daily = _daily(
            [("2026-09-07", 10.0, 100.0), ("2026-09-08", 11.0, 200.0)]
        )
        service = _make_service(monkeypatch, _FakeProvider(), test_db)
        _seed_symbol(service, test_db, "AAA.SS", daily)
        # 既有历史数据保留（停写 ≠ 清表）
        service.refresh_fitted_period_bars("AAA.SS", full=True)
        assert len(test_db.load_fitted_period_bars("AAA.SS", "1w")) == 2
        monkeypatch.setattr(service, "refresh_fitted_period_bars", MagicMock())

        result = service.ensure_daily_history(
            "AAA.SS", date(2026, 1, 1), date(2026, 9, 8),
            factors=[], factors_changed=True,
        )

        assert result["factors_changed"] is True
        service.refresh_fitted_period_bars.assert_not_called()
