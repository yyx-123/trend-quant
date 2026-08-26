"""P2-1/P2-2 回归测试。

- P2-1：引擎 `_prepare_bars` / `_filter_bars` 在 date/time 两列俱缺时
  必须抛业务错误（旧实现走 else 分支直接 KeyError）。
- P2-2：报价缓存键以归一化 symbol 为准——调用方传裸码/小写/.SH 后缀
  与传归一化 symbol 必须命中同一份缓存。
"""

from __future__ import annotations

import pandas as pd
import pytest

from rule_backtest.engine import SingleSymbolAllInBacktestEngine
from rule_backtest.service import RuleBacktestService


class TestPrepareBarsMissingTimeColumn:
    def test_engine_prepare_bars_raises(self) -> None:
        bars = pd.DataFrame({"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05]})
        with pytest.raises(ValueError, match="缺少时间列"):
            SingleSymbolAllInBacktestEngine._prepare_bars(bars)

    def test_service_filter_bars_raises(self) -> None:
        bars = pd.DataFrame({"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05]})
        with pytest.raises(ValueError, match="缺少时间列"):
            RuleBacktestService._filter_bars(bars, None, None)

    def test_time_column_still_accepted(self) -> None:
        bars = pd.DataFrame({
            "time": ["2025-01-01"],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.05],
        })
        prepared = SingleSymbolAllInBacktestEngine._prepare_bars(bars)
        assert len(prepared) == 1
        filtered = RuleBacktestService._filter_bars(bars, None, None)
        assert len(filtered) == 1


class TestQuoteCacheKeyNormalization:
    def test_fetch_latest_quote_normalizes_symbol(self, monkeypatch) -> None:
        import data.service as data_service

        data_service._quote_cache.clear()
        calls: list[str] = []

        class FakeProvider:
            def fetch_latest_quote(self, symbol: str) -> dict:
                calls.append(symbol)
                return {"symbol": symbol, "price": 1.0, "name": "测试"}

        service = data_service.DataService.__new__(data_service.DataService)
        monkeypatch.setattr(
            service, "_tickflow_provider", lambda: ("tickflow", FakeProvider())
        )

        first = service.fetch_latest_quote("510300")
        assert first["price"] == 1.0
        assert calls == ["510300.SS"]
        # 缓存键为归一化 symbol：等值写法必须命中缓存、不再打 provider
        second = service.fetch_latest_quote("510300.sh")
        assert second == first
        assert calls == ["510300.SS"]
        data_service._quote_cache.clear()

    def test_fetch_latest_quotes_normalizes_symbols(self, monkeypatch) -> None:
        import data.service as data_service

        data_service._quote_cache.clear()
        seen: list[list[str]] = []

        class FakeProvider:
            def fetch_latest_quotes(self, symbols: list[str]) -> dict:
                seen.append(list(symbols))
                return {s: {"symbol": s, "price": 1.0} for s in symbols}

        service = data_service.DataService.__new__(data_service.DataService)
        monkeypatch.setattr(
            service, "_tickflow_provider", lambda: ("tickflow", FakeProvider())
        )

        result = service.fetch_latest_quotes(["510300", "159915.sz"])
        assert seen == [["510300.SS", "159915.SZ"]]
        assert set(result) == {"510300.SS", "159915.SZ"}
        # 第二次全部命中缓存，不再发网络请求
        again = service.fetch_latest_quotes(["510300.SS", "159915.SZ"])
        assert set(again) == {"510300.SS", "159915.SZ"}
        assert len(seen) == 1
        data_service._quote_cache.clear()
