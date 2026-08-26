"""Unit tests for core.benchmarks — 存活接口（P2-6 死 API 已删）。"""

from __future__ import annotations

from core.benchmarks import benchmark_instruments, benchmark_market_symbols


class TestBenchmarkUtilities:
    def test_market_symbols(self) -> None:
        symbols = benchmark_market_symbols()
        assert "510500.SS" in symbols
        assert "159915.SZ" in symbols

    def test_instruments(self) -> None:
        instruments = benchmark_instruments()
        assert {i["symbol"] for i in instruments} == {"510500.SS", "159915.SZ"}
        for item in instruments:
            assert item["name"]
            assert item["benchmark_mode"] in ("csi500", "chinext")
