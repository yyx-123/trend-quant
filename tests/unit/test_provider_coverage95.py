"""provider_tickflow 覆盖率补测（目标 ≥95%）：客户端缺失错误、规整边界、
报价各返回形态、ex_factors 键回退。"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.provider_tickflow import TickFlowProvider


class TestNoClientErrors:
    """未配置 API key 时各抓取入口的明确报错（provider_tickflow.py:203/270/359/407）。"""

    @patch.dict(os.environ, {}, clear=True)
    def test_all_entries_raise_clear_error(self) -> None:
        os.environ.pop("TICKFLOW_API_KEY", None)
        provider = TickFlowProvider()
        with pytest.raises(RuntimeError, match="TICKFLOW_API_KEY"):
            provider.fetch_daily_history("510300.SS", date(2026, 8, 1), date(2026, 8, 21), "qfq")
        with pytest.raises(RuntimeError, match="TICKFLOW_API_KEY"):
            provider.fetch_daily_histories(["510300.SS"], date(2026, 8, 1), date(2026, 8, 21), "qfq")
        with pytest.raises(RuntimeError, match="TICKFLOW_API_KEY"):
            provider.fetch_ex_factors(["510300.SS"])
        with pytest.raises(RuntimeError, match="TICKFLOW_API_KEY"):
            provider.fetch_latest_quotes(["510300.SS"])
        with pytest.raises(RuntimeError, match="TICKFLOW_API_KEY"):
            provider.fetch_instrument_name("510300.SS")


class TestNormalizeKlinesEdges:
    def test_empty_and_none(self) -> None:
        assert TickFlowProvider._normalize_klines(None, "X.SS").empty
        assert TickFlowProvider._normalize_klines([], "X.SS").empty

    def test_compact_missing_fields_default_fill(self) -> None:
        """compact 格式缺列时按默认填充（provider_tickflow.py:128-132）。"""
        result = TickFlowProvider._compact_klines_to_dataframe(
            {"timestamp": [1751212800000], "close": [8.4]},
            "518850.SS",
        )
        assert len(result) == 1
        assert result.iloc[0]["close"] == 8.4
        assert result.iloc[0]["amount"] == 0.0  # 缺省 0.0

    def test_compact_empty_timestamps(self) -> None:
        result = TickFlowProvider._compact_klines_to_dataframe({"timestamp": []}, "X.SS")
        assert result.empty


class TestFetchDailyHistoryEdges:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_start_end_swapped_and_mask(self, tickflow_cls: MagicMock) -> None:
        """start>end 自动交换（provider_tickflow.py:154-155）；区间掩码过滤。"""
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame([
            {"trade_date": "2026-08-20", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
            {"trade_date": "2026-08-25", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ])
        result = provider.fetch_daily_history(
            "510300.SS", date(2026, 8, 21), date(2026, 8, 19), "qfq"
        )
        assert len(result) == 1
        assert str(result.iloc[0]["time"])[:10] == "2026-08-20"

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_empty_result_returned_directly(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.get.return_value = pd.DataFrame()
        result = provider.fetch_daily_history("510300.SS", date(2026, 8, 1), date(2026, 8, 21), "qfq")
        assert result.empty

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_vendor_error_wrapped(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.get.side_effect = RuntimeError("vendor boom")
        with pytest.raises(RuntimeError, match="tickflow daily fetch failed"):
            provider.fetch_daily_history("510300.SS", date(2026, 8, 1), date(2026, 8, 21), "qfq")


class TestFetchExFactorsEdges:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_local_symbol_key_fallback(self, tickflow_cls: MagicMock) -> None:
        """返回 map 用项目代码（而非 tickflow 代码）做键时的回退（provider_tickflow.py:283-285）。"""
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.ex_factors.return_value = {
            "510300.SS": [{"ex_factor": 1.1, "timestamp": 1754016000000}],
        }
        factors, errors = provider.fetch_ex_factors(["510300.SS"])
        assert errors == {}
        assert len(factors["510300.SS"]) == 1

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_non_dict_raw_treated_empty(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.klines.ex_factors.return_value = ["not-a-dict"]
        factors, _errors = provider.fetch_ex_factors(["510300.SS"])
        assert factors["510300.SS"] == []

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_empty_symbols_shortcircuit(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        assert provider.fetch_ex_factors([]) == ({}, {})
        assert provider.fetch_ex_factors(["", "  "]) == ({}, {})


class TestFetchLatestQuoteShapes:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_dataframe_list_dict_and_other(self, tickflow_cls: MagicMock) -> None:
        """单只报价的四种返回形态（provider_tickflow.py:316-324）。"""
        provider = TickFlowProvider()
        client = tickflow_cls.return_value

        client.quotes.get.return_value = pd.DataFrame([
            {"symbol": "510300.SH", "last_price": 4.2, "name": "x"}
        ])
        assert provider.fetch_latest_quote("510300.SS")["price"] == 4.2

        client.quotes.get.return_value = [{"symbol": "510300.SH", "last_price": 4.3, "name": "x"}]
        assert provider.fetch_latest_quote("510300.SS")["price"] == 4.3

        client.quotes.get.return_value = {"symbol": "510300.SH", "last_price": 4.4, "name": "x"}
        assert provider.fetch_latest_quote("510300.SS")["price"] == 4.4

        client.quotes.get.return_value = "garbage"
        result = provider.fetch_latest_quote("510300.SS")
        assert result["price"] is None  # 非预期形态 → 空 item


class TestFetchLatestQuotesShapes:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_empty_input_returns_empty(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        assert provider.fetch_latest_quotes([]) == {}

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_dataframe_and_dict_and_dupes(self, tickflow_cls: MagicMock) -> None:
        """批量报价：DataFrame 形态、重复/非 dict 行过滤、未返回标的补 error。"""
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.quotes.get.return_value = pd.DataFrame([
            {"symbol": "510300.SH", "last_price": 4.2, "name": "x", "trade_time": "t"},
            {"symbol": "510300.SH", "last_price": 4.3, "name": "x", "trade_time": "t"},  # 重复去重
        ])
        result = provider.fetch_latest_quotes(["510300.SS", "510500.SS"])
        assert result["510300.SS"]["price"] == 4.2
        assert result["510500.SS"]["error"] == "no quote returned"

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_single_dict_wrapped(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.quotes.get.return_value = {"symbol": "510300.SH", "last_price": 4.2, "name": "x"}
        result = provider.fetch_latest_quotes(["510300.SS"])
        assert result["510300.SS"]["price"] == 4.2


class TestFetchInstrumentNameEdges:
    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_non_dict_and_blank_name(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.instruments.get.return_value = "not-a-dict"
        assert provider.fetch_instrument_name("510300.SS") is None
        client.instruments.get.return_value = {"name": "  "}
        assert provider.fetch_instrument_name("510300.SS") is None

    @patch.dict(os.environ, {"TICKFLOW_API_KEY": "k"}, clear=True)
    @patch("data.provider_tickflow.TickFlow")
    def test_error_wrapped(self, tickflow_cls: MagicMock) -> None:
        provider = TickFlowProvider()
        client = tickflow_cls.return_value
        client.instruments.get.side_effect = RuntimeError("down")
        with pytest.raises(RuntimeError, match="tickflow instrument fetch failed"):
            provider.fetch_instrument_name("510300.SS")


class TestThrottle:
    def test_zero_interval_noop(self) -> None:
        """interval <= 0 直接返回（provider_tickflow.py:92）。"""
        import data.provider_tickflow as pt

        pt._NEXT_REQUEST_AT.clear()
        provider = pt.TickFlowProvider()
        provider._throttle("noop_op", 0.0)
        assert "noop_op" not in pt._NEXT_REQUEST_AT


class TestFromTickflowSymbol:
    def test_sh_to_ss(self) -> None:
        assert TickFlowProvider._from_tickflow_symbol("510300.SH") == "510300.SS"
        assert TickFlowProvider._from_tickflow_symbol("159915.SZ") == "159915.SZ"
