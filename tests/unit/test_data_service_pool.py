"""附录 A（H5/H6）：data/service.py 与 data/provider_tickflow.py 关键路径补测。

service：update_pool_daily 计数、_retry_wait_seconds 解析、
_non_retryable_provider_error、ensure_daily_history 短路、sync_ex_factors 落库。
provider：TICKFLOW_BASE_URL 镜像、fetch_ex_factors 分批/UTC 口径、
fetch_latest_quotes 分块与错误兜底。
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import data.service as data_service
from data.provider_tickflow import TickFlowProvider
from data.service import _non_retryable_provider_error, _retry_wait_seconds


# ---------------------------------------------------------------------------
# 重试辅助
# ---------------------------------------------------------------------------
class TestRetryHelpers:
    def test_retry_wait_parses_ms_hint(self) -> None:
        errors = {"A": "请求频率超限，请 2500 ms 后重试", "B": "请 1000 ms 后重试"}
        assert _retry_wait_seconds(errors, 5.0) == 5.0  # fallback 更大
        assert _retry_wait_seconds(errors, 1.0) == 2.5  # 取最大等待

    def test_retry_wait_fallback_when_no_hint(self) -> None:
        assert _retry_wait_seconds({"A": "unknown error"}, 3.0) == 3.0

    def test_non_retryable_markers(self) -> None:
        assert _non_retryable_provider_error({"A": "无日/周/月K线查询批量查询权限"}) is not None
        assert _non_retryable_provider_error({"A": "403 Forbidden"}) is not None
        assert _non_retryable_provider_error({"A": "网络超时"}) is None


# ---------------------------------------------------------------------------
# update_pool_daily 汇总
# ---------------------------------------------------------------------------
class TestUpdatePoolDaily:
    def test_counts_and_job_run(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        service = data_service.DataService.__new__(data_service.DataService)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols: ({}, []))

        outcomes = {
            "AAA.SS": {"symbol": "AAA.SS", "status": "updated"},
            "BBB.SS": {"symbol": "BBB.SS", "status": "no_data"},
        }

        def fake_ensure(symbol, *args, **kwargs):
            if symbol == "CCC.SS":
                raise RuntimeError("boom")
            return outcomes[symbol]

        monkeypatch.setattr(service, "ensure_daily_history", fake_ensure)

        payload = service.update_pool_daily(
            symbols=["AAA.SS", "BBB.SS", "CCC.SS"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 21),
            max_retries=0,
        )
        assert payload["total"] == 3
        assert payload["success"] == 1  # no_data 与 error 都不算 success
        assert payload["failed"] == 1
        assert payload["failed_symbols"] == ["CCC.SS"]

        run = test_db.get_latest_job_run("daily_update")
        assert run is not None
        assert run["status"] == "partial"
        assert run["run_date"] == "2026-08-21"


# ---------------------------------------------------------------------------
# ensure_daily_history 短路
# ---------------------------------------------------------------------------
class TestEnsureDailyHistoryShortCircuit:
    def test_up_to_date_skips_provider(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        # 本地数据已覆盖请求区间
        bars = pd.DataFrame([
            {"time": f"2026-08-{10 + i:02d}", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}
            for i in range(10)
        ])
        test_db.save_market_data("AAA.SS", bars, price_mode="raw")

        service = data_service.DataService.__new__(data_service.DataService)
        service.market_store = data_service.MarketStore(db=test_db)
        service.raw_store = data_service.MarketStore(db=test_db, price_mode="raw")

        def _no_provider(*args, **kwargs):
            raise AssertionError("provider should not be called")

        monkeypatch.setattr(service, "fetch_daily_history", _no_provider)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols, **kw: ({}, []))

        result = service.ensure_daily_history(
            "AAA.SS", date(2026, 8, 10), date(2026, 8, 19)
        )
        assert result["status"] == "up_to_date"


# ---------------------------------------------------------------------------
# sync_ex_factors
# ---------------------------------------------------------------------------
class TestSyncExFactors:
    def test_new_factors_persisted_and_reported(self, test_db, monkeypatch: pytest.MonkeyPatch) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        service = data_service.DataService.__new__(data_service.DataService)
        factors = [(date(2026, 8, 1), 1.05)]
        monkeypatch.setattr(service, "fetch_ex_factors", lambda symbols: ({"AAA.SS": factors}, {}))

        _fetched, changed = service.sync_ex_factors(["AAA.SS"], db=test_db)
        assert changed == ["AAA.SS"]
        stored = test_db.load_ex_factors("AAA.SS")
        assert len(stored) == 1
        assert float(stored[0][1]) == 1.05
        assert str(stored[0][0])[:10] == "2026-08-01"

        # 再次同步：无变化
        _fetched2, changed2 = service.sync_ex_factors(["AAA.SS"], db=test_db)
        assert changed2 == []


# ---------------------------------------------------------------------------
# provider：镜像 base_url / 分批 / 分块
# ---------------------------------------------------------------------------
class TestProviderConfig:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k", "TICKFLOW_BASE_URL": "https://mirror.example.com"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_base_url_mirror_override(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        provider._get_client()
        tickflow_cls.assert_called_once_with(api_key="k", base_url="https://mirror.example.com")


class TestFetchExFactors:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_batching_and_utc_dates(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        calls: list[list[str]] = []

        def fake_batch(tickflow_symbols):
            calls.append(list(tickflow_symbols))
            # 一条缺 timestamp（应跳过），一条 UTC 毫秒时间戳
            return {
                s: [
                    {"ex_factor": 1.1},  # no timestamp → skip
                    {"ex_factor": 1.2, "timestamp": 1754016000000},  # 2025-08-01 UTC
                ]
                for s in tickflow_symbols
            }

        client.klines.ex_factors.side_effect = fake_batch

        symbols = [f"{600000 + i}.SS" for i in range(120)]
        factors, errors = provider.fetch_ex_factors(symbols)
        # 120 标的、每批 50 → 3 批
        assert len(calls) == 3
        assert len(calls[0]) == 50 and len(calls[2]) == 20
        assert errors == {}
        sample = factors["600000.SS"]
        assert len(sample) == 1  # 缺 timestamp 的 entry 被跳过
        assert sample[0][0].isoformat() == "2025-08-01"  # UTC 毫秒 → UTC 日期
        assert float(sample[0][1]) == 1.2


class TestFetchLatestQuotes:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_chunking_and_error_fallback(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        calls: list[list[str]] = []

        def fake_quotes(symbols):
            calls.append(list(symbols))
            if len(calls) == 1:
                raise RuntimeError("chunk exploded")
            # 第二块只返回第一个标的（tickflow 代码），其余缺
            return [{"symbol": symbols[0], "last_price": 1.0, "name": "x", "trade_time": "2026-08-25T10:00:00"}]

        client.quotes.get.side_effect = fake_quotes

        symbols = [f"{600000 + i}.SS" for i in range(51)]
        result = provider.fetch_latest_quotes(symbols)
        assert len(calls) == 2  # 51 → 2 chunk（50 + 1）
        # 第一块全部 error 且不中断
        assert all(result[s]["error"] == "chunk exploded" for s in symbols[:50])
        # 第二块：被返回的标的有价
        assert result[symbols[50]]["price"] == 1.0
