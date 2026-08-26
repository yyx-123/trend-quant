"""咽喉模块覆盖率补测（三）：data/service 报价缓存/ensure_daily_history 细节、
update_pool_daily 重试、provider_tickflow 日K规整与单只报价。"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import data.service as ds_mod
from tests.unit.test_throat_coverage2 import _bars, _FakeProvider, _service


class TestQuoteCache:
    def test_ttl_cache_hit_skips_provider(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        provider = _FakeProvider()
        calls: list[str] = []
        orig = provider.fetch_latest_quote

        def counted(symbol):
            calls.append(symbol)
            return orig(symbol)

        provider.fetch_latest_quote = counted
        service = _service(monkeypatch, test_db, provider)
        ds_mod._quote_cache.clear()

        first = service.fetch_latest_quote("510300.SS")
        second = service.fetch_latest_quote("510300.SS")
        assert first == second
        assert calls == ["510300.SS"]  # 第二次命中缓存

        # 批量：部分命中，只为缺失的发请求
        batch_calls: list[list[str]] = []
        provider.fetch_latest_quotes = lambda symbols: batch_calls.append(list(symbols)) or {
            s: {"symbol": s, "price": 2.0} for s in symbols
        }
        service.fetch_latest_quotes(["510300.SS", "510500.SS"])
        assert batch_calls == [["510500.SS"]]
        ds_mod._quote_cache.clear()

    def test_error_quote_not_cached(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        from data.service import DataProviderError

        provider = _FakeProvider()
        provider.fetch_latest_quote = lambda s: {"symbol": s, "error": "no quote returned"}
        service = _service(monkeypatch, test_db, provider)
        ds_mod._quote_cache.clear()
        # 无价格报价：抛 DataProviderError 且不写缓存
        with pytest.raises(DataProviderError):
            service.fetch_latest_quote("510300.SS")
        assert ds_mod._quote_cache == {}


class TestFetchInstrumentName:
    def test_delegates_to_provider(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        provider = _FakeProvider()
        provider.fetch_instrument_name = lambda s: "沪深300ETF"
        service = _service(monkeypatch, test_db, provider)
        assert service.fetch_instrument_name("510300.SS")["name"] == "沪深300ETF"


class TestEnsureDailyHistoryDetails:
    def test_fetches_missing_and_appends(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """本地为空 → 全量抓取并落 raw + qfq 物化。"""
        service = _service(monkeypatch, test_db)
        result = service.ensure_daily_history("510300.SS", date(2026, 8, 17), date(2026, 8, 21))
        assert result["status"] == "updated"
        assert not test_db.load_market_data("510300.SS", price_mode="qfq").empty

    def test_fetch_failure_propagates(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        service = _service(monkeypatch, test_db, _FakeProvider(fail=True))
        with pytest.raises(RuntimeError, match="vendor down"):
            service.ensure_daily_history("510300.SS", date(2026, 8, 17), date(2026, 8, 21))


class TestUpdatePoolDailyRetry:
    def test_retry_then_success(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: test_db)
        monkeypatch.setattr(db_module, "_db_instance", test_db)

        service = _service(monkeypatch, test_db)
        monkeypatch.setattr(service, "sync_ex_factors", lambda symbols: ({}, []))

        attempts: list[int] = []

        def flaky(symbol, *args, **kwargs):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient")
            return {"symbol": symbol, "status": "updated"}

        monkeypatch.setattr(service, "ensure_daily_history", flaky)
        monkeypatch.setattr(ds_mod.time, "sleep", lambda s: None)

        payload = service.update_pool_daily(
            symbols=["510300.SS"],
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 21),
            max_retries=2,
        )
        assert payload["success"] == 1
        assert payload["failed"] == 0
        assert len(attempts) == 2


# ---------------------------------------------------------------------------
# provider_tickflow：日K规整（时区转换）与单只报价
# ---------------------------------------------------------------------------
class TestProviderKlines:
    def test_compact_klines_epoch_to_shanghai_date(self) -> None:
        """compact 线格式（dict-of-arrays，批量路径）：UTC 毫秒 → Asia/Shanghai 墙钟。"""
        from data.provider_tickflow import TickFlowProvider

        # 2025-06-29 16:00 UTC = 2025-06-30 00:00 Asia/Shanghai
        result = TickFlowProvider._compact_klines_to_dataframe(
            {
                "timestamp": [1751212800000],
                "open": [8.4], "high": [8.5], "low": [8.3], "close": [8.4],
                "volume": [100], "amount": [840],
            },
            "518850.SS",
        )
        assert len(result) == 1
        assert str(result.iloc[0]["time"])[:10] == "2025-06-30"

    def test_normalize_klines_list_format(self) -> None:
        """list-of-dicts 线格式规整：trade_date/trade_time 单列均可识别。"""
        from data.provider_tickflow import TickFlowProvider

        by_trade_date = TickFlowProvider._normalize_klines(
            [
                {"symbol": "518850.SH", "trade_date": "2026-06-25", "open": 8.4, "high": 8.5, "low": 8.3, "close": 8.4, "volume": 100, "amount": 840},
            ],
            "518850.SS",
        )
        assert len(by_trade_date) == 1
        assert str(by_trade_date.iloc[0]["time"])[:10] == "2026-06-25"

        by_trade_time = TickFlowProvider._normalize_klines(
            [
                {"symbol": "518850.SH", "trade_time": "2026-06-24T16:00:00+00:00", "open": 8.4, "high": 8.5, "low": 8.3, "close": 8.4, "volume": 100, "amount": 840},
            ],
            "518850.SS",
        )
        assert len(by_trade_time) == 1
        # 时区统一东八区：UTC 16:00 → 北京时间次日 00:00（与 compact 路径一致）
        assert str(by_trade_time.iloc[0]["time"]) == "2026-06-25 00:00:00"

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_latest_quote_normalizes(self, tickflow_cls: MagicMock) -> None:
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.quotes.get.return_value = [
            {
                "symbol": "510300.SH", "name": "沪深300ETF", "last_price": 4.2,
                "open": 4.1, "high": 4.3, "low": 4.0, "volume": 100, "amount": 420,
                "trade_time": "2026-08-25T10:00:00",
            }
        ]
        quote = provider.fetch_latest_quote("510300.SS")
        assert quote["price"] == 4.2
        assert quote["symbol"] == "510300.SS"
        assert quote["name"] == "沪深300ETF"

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_fetch_latest_quote_error_raises(self, tickflow_cls: MagicMock) -> None:
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.quotes.get.side_effect = RuntimeError("down")
        with pytest.raises(RuntimeError, match="510300"):
            provider.fetch_latest_quote("510300.SS")

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_daily_histories_chunk_errors(self, tickflow_cls: MagicMock) -> None:
        """批量日K：单 chunk 异常不中断，错误按标的落 errors。"""
        from data.provider_tickflow import TickFlowProvider

        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        calls: list[list[str]] = []

        def fake_batch(symbols, **kw):
            symbols = list(symbols)
            calls.append(symbols)
            if len(calls) == 1:
                raise RuntimeError("chunk boom")
            return pd.DataFrame([
                {"symbol": symbols[0], "trade_date": "2026-08-20", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}
            ])

        client.klines.batch.side_effect = fake_batch
        symbols = [f"{600000 + i}.SS" for i in range(101)]
        _data, errors = provider.fetch_daily_histories(
            symbols, date(2026, 8, 1), date(2026, 8, 21), "qfq"
        )
        assert len(calls) == 2  # 101 → 2 chunk（100 + 1）
        assert all(s in errors for s in symbols[:100])
        assert "chunk boom" in errors[symbols[0]]


class TestBatchUpToDateBranch:
    def test_up_to_date_symbol_marked(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """本地已覆盖的标的在批量回填中直接记 up_to_date（跳过网络）。"""
        provider = _FakeProvider()
        service = _service(monkeypatch, test_db, provider)
        test_db.save_market_data("510300.SS", _bars(5), price_mode="raw")
        events: list[dict] = []
        results = service.backfill_daily_histories(
            [{"symbol": "510300.SS", "start_date": date(2026, 8, 17)}],
            end_date=date(2026, 8, 19),
            progress_callback=lambda e: events.append(e),
        )
        assert results[0]["ok"] is True
        assert results[0]["result"]["status"] == "up_to_date"
        assert any(e["event"] == "item_done" for e in events)


class TestFetchInstrumentNameViaQuote:
    def test_name_from_quote(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        provider = _FakeProvider()
        provider.fetch_latest_quote = lambda s: {"symbol": s, "price": 1.0, "name": "沪深300ETF", "ts": "2026-08-25T10:00:00"}
        service = _service(monkeypatch, test_db, provider)
        result = service.fetch_instrument_name("510300.SS")
        assert result["name"] == "沪深300ETF"
        assert result["provider"] == "tickflow"

    def test_no_name_raises(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        from data.service import DataProviderError

        provider = _FakeProvider()
        provider.fetch_latest_quote = lambda s: {"symbol": s, "price": 1.0, "name": ""}
        service = _service(monkeypatch, test_db, provider)
        with pytest.raises(DataProviderError):
            service.fetch_instrument_name("510300.SS")


class TestFetchHistoriesFallback:
    def test_per_symbol_fallback_when_no_batch_method(self, monkeypatch: pytest.MonkeyPatch, test_db) -> None:
        """provider 无 fetch_daily_histories 方法时退化为逐标的循环。"""

        class SingleOnlyProvider:
            def fetch_daily_history(self, symbol, start, end, adjust):
                if symbol == "BAD.SS":
                    raise RuntimeError("no such symbol")
                return _bars(3)

        service = _service(monkeypatch, test_db, SingleOnlyProvider())
        data, errors = service.fetch_daily_histories(
            ["510300.SS", "BAD.SS"], date(2026, 8, 1), date(2026, 8, 21)
        )
        assert "510300.SS" in data
        assert errors["BAD.SS"] == "no such symbol"
        assert (data["510300.SS"]["provider"] == "tickflow").all()
