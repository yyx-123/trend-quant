"""基准标的常量（中证500/创业板指数 ETF）。

历史回测对比基准的模式化 API（BENCHMARK_OPTIONS / normalize_benchmark_mode
等）已随旧回测页退役删除（P2-6，全仓零调用）；存活接口只有两个：
``benchmark_market_symbols``（日更池兜底标的）与 ``benchmark_instruments``
（标的管理页的基准清单）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkOption:
    mode: str
    label: str
    symbol: str = ""
    instrument_name: str = ""


INDEX_BENCHMARKS: tuple[BenchmarkOption, ...] = (
    BenchmarkOption(
        mode="csi500",
        label="中证500指数",
        symbol="510500.SS",
        instrument_name="中证500ETF南方",
    ),
    BenchmarkOption(
        mode="chinext",
        label="创业板指数",
        symbol="159915.SZ",
        instrument_name="创业板ETF易方达",
    ),
)


def benchmark_market_symbols() -> list[str]:
    return [item.symbol for item in INDEX_BENCHMARKS if item.symbol]


def benchmark_instruments() -> list[dict]:
    return [
        {
            "symbol": item.symbol,
            "name": item.instrument_name or item.label,
            "benchmark_mode": item.mode,
        }
        for item in INDEX_BENCHMARKS
        if item.symbol
    ]
