"""批量回测耗时实测脚本（方案 §5.6 第 0 步）。

按 bar_count 分层抽样（<500 / 500-2000 / 2000-5000 / 5000+），每层抽取若干标的，
用同一策略跑单标的回测并计时，输出单格耗时分布，用于校准批量回测的耗时预估。

用法: .venv/Scripts/python.exe scripts/bench_backtest_timing.py [--per-bucket 6] [--strategy-id <id>]
只读真实数据库，不写任何数据。
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data.storage.db import init_db
from data.storage.market_store import MarketStore
from rule_backtest.engine import SingleSymbolAllInBacktestEngine
from rule_backtest.loader import StrategyLoader
from rule_backtest.models import BacktestExecutionConfig, RuleBacktestRequest

BUCKETS = [("<500", 0, 500), ("500-2000", 500, 2000), ("2000-5000", 2000, 5000), ("5000+", 5000, 10**9)]


def symbol_bar_counts(db_path: str) -> list[tuple[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT symbol, COUNT(*) FROM market_data_qfq GROUP BY symbol"
        ).fetchall()
    finally:
        conn.close()


def stratified_sample(
    counts: list[tuple[str, int]], per_bucket: int, seed: int = 42
) -> dict[str, list[tuple[str, int]]]:
    rng = random.Random(seed)
    sample: dict[str, list[tuple[str, int]]] = {}
    for label, lo, hi in BUCKETS:
        members = [(s, c) for s, c in counts if lo <= c < hi]
        sample[label] = rng.sample(members, min(per_bucket, len(members)))
    return sample


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-bucket", type=int, default=6)
    parser.add_argument("--strategy-id", type=str, default="")
    parser.add_argument("--db", type=str, default="data/trend_quant.db")
    args = parser.parse_args()

    init_db(args.db)
    loader = StrategyLoader()
    if args.strategy_id:
        strategy = loader.load(args.strategy_id)
    else:
        # 默认用第一个含指标较多的活跃策略（典型趋势策略）
        strategies = [s for s in loader.list_strategies() if s.get("is_active", True)]
        strategy = loader.load(str(strategies[0]["id"]))
    print(f"策略: {strategy.get('name', '')} ({strategy.get('id', '')})")

    counts = symbol_bar_counts(args.db)
    sample = stratified_sample(counts, args.per_bucket)

    store = MarketStore()
    engine = SingleSymbolAllInBacktestEngine()
    execution = BacktestExecutionConfig()

    all_timings: list[float] = []
    print(f"\n{'层级':<12}{'标的':<14}{'bar数':>8}{'加载s':>8}{'回测s':>8}")
    for label, members in sample.items():
        bucket_timings: list[float] = []
        for symbol, bar_count in members:
            t0 = time.perf_counter()
            bars = store.load_history(symbol)
            t1 = time.perf_counter()
            result = engine.run(
                RuleBacktestRequest(
                    strategy=strategy, symbol=symbol, bars=bars, execution=execution
                )
            )
            t2 = time.perf_counter()
            load_s, run_s = t1 - t0, t2 - t1
            bucket_timings.append(run_s)
            all_timings.append(run_s)
            status = result.get("status", "?")
            print(f"{label:<12}{symbol:<14}{len(bars):>8}{load_s:>8.2f}{run_s:>8.2f}  {status}")
        if bucket_timings:
            print(
                f"  → {label} 均值 {statistics.mean(bucket_timings):.2f}s, "
                f"p90 {sorted(bucket_timings)[int(len(bucket_timings) * 0.9)]:.2f}s"
            )

    if all_timings:
        s = sorted(all_timings)
        print(f"\n总体: n={len(s)}, 均值 {statistics.mean(s):.2f}s, "
              f"中位 {s[len(s)//2]:.2f}s, p90 {s[int(len(s)*0.9)]:.2f}s, max {s[-1]:.2f}s")
        print("外推（单格耗时 × 格子数）请以分层均值 × 各层标的数估算。")


if __name__ == "__main__":
    main()
