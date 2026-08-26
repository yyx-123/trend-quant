"""data/service.py 覆盖率补测（目标 ≥95%）：报价缓存细节、批量兜底、
ensure_daily_history 与批量回填的分支语义。"""

from __future__ import annotations

from datetime import date

import pytest

import data.service as ds_mod
from data.service import DataProviderError
from tests.unit.test_throat_coverage2 import _bars, _FakeProvider, _service


class TestQuoteCacheEdges:
    def test_empty_key_cache_helpers(self) -> None:
        """空 key：get 返回 None、put 静默不写（service.py:56-57, 67-68）。"""
        ds_mod._quote_cache.clear()
        assert ds_mod._quote_cache_get("") is None
        ds_mod._quote_cache_put("", {"symbol": "", "price": 1.0})
        assert ds_mod._quote_cache == {}


class TestFetchLatestQuotesFallback:
    def test_single_loop_fallback(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """provider 无批量方法时逐标的循环（service.py:206-212）。"""

        class SingleOnlyProvider:
            def fetch_latest_quote(self, symbol):
                if symbol == "BAD.SS":
                    raise RuntimeError("no quote")
                return {"symbol": symbol, "price": 1.5, "name": "x"}

        service = _service(monkeypatch, test_db, SingleOnlyProvider())
        ds_mod._quote_cache.clear()
        result = service.fetch_latest_quotes(["510300.SS", "BAD.SS"])
        assert result["510300.SS"]["price"] == 1.5
        assert result["BAD.SS"]["error"] == "no quote"
        ds_mod._quote_cache.clear()

    def test_empty_input_returns_empty(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        service = _service(monkeypatch, test_db)
        assert service.fetch_latest_quotes([]) == {}


class TestEnsureDailyHistoryInternals:
    def test_provider_error_treated_as_no_increment(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """抓取 DataProviderError（区间无新 bar）视为无增量（service.py:342-344）。"""
        provider = _FakeProvider()
        provider.fetch_daily_history = lambda *a, **k: (_ for _ in ()).throw(DataProviderError("no data"))
        service = _service(monkeypatch, test_db, provider)
        test_db.save_market_data("510300.SS", _bars(3), price_mode="raw")
        result = service.ensure_daily_history("510300.SS", date(2026, 8, 17), date(2026, 8, 21))
        # 区间无新 bar 且本地已覆盖 → 唯一语义是 up_to_date
        assert result["status"] == "up_to_date"

    def test_ex_factor_sync_failure_uses_stored(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """因子同步失败 → 用本地因子表（service.py:356-362）。"""
        service = _service(monkeypatch, test_db)
        monkeypatch.setattr(
            service,
            "sync_ex_factors",
            lambda symbols, db=None: (_ for _ in ()).throw(RuntimeError("vendor down")),
        )
        result = service.ensure_daily_history(
            "510300.SS", date(2026, 8, 17), date(2026, 8, 21), factors_changed=None
        )
        assert result["factors_changed"] is False

    def test_factors_changed_none_compares_stored(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """factors_changed=None 时与本地因子表 diff（service.py:363-364）。"""
        service = _service(monkeypatch, test_db)
        new_factors = [(date(2026, 8, 20), 1.2)]
        result = service.ensure_daily_history(
            "510300.SS",
            date(2026, 8, 17),
            date(2026, 8, 21),
            factors=new_factors,
            factors_changed=None,
        )
        # 传入因子与本地（空）不同 → changed=True
        assert result["factors_changed"] is True


class TestBackfillDailyHistoryEdges:
    def test_start_end_swapped(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """start>end 自动交换（service.py:401-402）。"""
        service = _service(monkeypatch, test_db)
        result = service.backfill_daily_history(
            symbol="510300.SS",
            start_date=date(2026, 8, 21),
            end_date=date(2026, 8, 17),
        )
        assert result["requested_start"] == "2026-08-17"
        assert result["requested_end"] == "2026-08-21"

    def test_rematerialize_failure_marked(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """qfq 重物化失败标记 failed 不中断（service.py:423-425）。"""
        service = _service(monkeypatch, test_db)
        monkeypatch.setattr(
            service,
            "rematerialize_qfq",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = service.backfill_daily_history(
            symbol="510300.SS",
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 21),
        )
        assert result["qfq_rematerialized"] == "failed"


class TestEffectiveFetchStart:
    def test_all_nat_times(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """本地时间为空/NaT 时从请求起点开始（service.py:436-439）。"""
        service = _service(monkeypatch, test_db)
        fetch_start, rows, _local_start, _local_end = service._effective_fetch_start(
            "NOPE.SS", date(2026, 8, 1)
        )
        assert fetch_start == date(2026, 8, 1)
        assert rows == 0


class TestBackfillHistoriesEdges:
    def test_skips_empty_and_duplicate_symbols(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """空 symbol 与重复 symbol 跳过（service.py:560-562）。"""
        service = _service(monkeypatch, test_db)
        results = service.backfill_daily_histories(
            [
                {"symbol": "", "start_date": date(2026, 8, 17)},
                {"symbol": "510300.SS", "start_date": date(2026, 8, 17)},
                {"symbol": "510300.SS", "start_date": date(2026, 8, 17)},
            ],
            end_date=date(2026, 8, 21),
        )
        assert len(results) == 1

    def test_start_date_as_string(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """start_date 支持字符串解析（service.py:563）。"""
        service = _service(monkeypatch, test_db)
        results = service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": "2026-08-17"}],
            end_date=date(2026, 8, 21),
        )
        assert results[0]["ok"] is True

    def test_request_start_progress_event(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """批量请求前发 request_start 事件（service.py:634-636）。"""
        service = _service(monkeypatch, test_db)
        events: list = []
        service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": date(2026, 8, 17)}],
            end_date=date(2026, 8, 21),
            progress_callback=lambda e: events.append(e),
        )
        kinds = {e["event"] for e in events}
        assert "request_start" in kinds

    def test_save_failure_marked_error(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """落库异常进 attempt_errors 并重试（service.py:682-685）。"""
        service = _service(monkeypatch, test_db)
        monkeypatch.setattr(
            service,
            "_save_backfill_result",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
        results = service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": date(2026, 8, 17)}],
            end_date=date(2026, 8, 21),
            max_retries=0,
        )
        assert results[0]["ok"] is False
        assert "disk full" in results[0]["error"]

    def test_non_retryable_marks_remaining(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """不可重试错误：剩余标的直接标记失败（service.py:699-713 前段）。"""
        provider = _FakeProvider()
        provider.fetch_daily_histories = lambda *a, **k: (
            {}, {s: "无日/周/月K线查询批量查询权限" for s in a[0]}
        )
        service = _service(monkeypatch, test_db, provider)
        results = service.backfill_daily_histories(
            [
                {"symbol": "510300.SS", "start_date": date(2026, 8, 17)},
                {"symbol": "510500.SS", "start_date": date(2026, 8, 17)},
            ],
            end_date=date(2026, 8, 21),
            max_retries=3,
        )
        assert all(not r["ok"] for r in results)
        assert any("批量查询权限" in r["error"] for r in results)
