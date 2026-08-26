"""Unit tests for data.provider_utils — OHLCV standardisation helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from data.provider_utils import safe_float, standardize_ohlcv


class TestSafeFloat:
    def test_is_core_trend_reexport(self) -> None:
        """provider_utils.safe_float 是 core.trend.safe_float 的再导出
        （P1-14 单一来源；具体语义由 test_smoke.TestSafeFloat 锁定）。"""
        import core.trend

        assert safe_float is core.trend.safe_float


class TestStandardizeOhlcv:
    def test_renames_columns(self) -> None:
        df = pd.DataFrame({
            "date": ["2025-01-01"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "成交量": [1_000_000],
            "amount": [10_500_000],
        })
        result = standardize_ohlcv(df, "TEST")
        assert "time" in result.columns
        assert "volume" in result.columns
        assert "open" in result.columns
        # 数值不丢失、时间不丢失、代码归属正确
        assert result["volume"].iloc[0] == 1_000_000
        assert result["open"].iloc[0] == 10.0
        assert result["close"].iloc[0] == 10.5
        assert str(result["time"].iloc[0].date()) == "2025-01-01"
        assert result["symbol"].iloc[0] == "TEST"

    def test_missing_time_column_raises(self) -> None:
        """P2-3：缺时间列必须显式报错，不允许伪造当前时间。"""
        df = pd.DataFrame({
            "trade_date": ["2025-01-01"],  # 非受识别的时间列别名
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
        })
        with pytest.raises(ValueError, match="缺少时间列"):
            standardize_ohlcv(df, "TEST")

    def test_sorts_by_time(self) -> None:
        df = pd.DataFrame({
            "date": ["2025-01-03", "2025-01-01", "2025-01-02"],
            "open": [10.0, 10.0, 10.0],
            "high": [11.0, 11.0, 11.0],
            "low": [9.0, 9.0, 9.0],
            "close": [10.5, 10.5, 10.5],
            "vol": [1_000_000, 1_000_000, 1_000_000],
            "amount": [10_500_000, 10_500_000, 10_500_000],
        })
        result = standardize_ohlcv(df, "TEST")
        times = result["time"].tolist()
        assert times == sorted(times)
