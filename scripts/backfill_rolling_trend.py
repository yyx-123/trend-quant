"""滚动周/月趋势值（trend_rolling_daily）全量回填脚本（幂等，可重复执行）。

从 market_data_qfq（日K 前复权）逐标的计算滚动锚定周/月趋势值
（core.rolling_bars.rolling_period_trend_series；周 D=5 ATR8/ER4/vol8，
月 D=22 ATR6/ER3/vol6，回看 K=16 根 bar），整段重建写入
trend_rolling_daily。用途：

- 新功能上线时首次把趋势值表建满；
- 趋势值表数据异常后的修复性重建（``--symbols`` 可限定标的）。

日常维护不需要本脚本：日更任务（ensure_daily_history）会为更新的标的
增量补行、除权因子变化时整段重建（见 docs/26-09-13-rolling-trend/）。

标的池与相位迁移研究脚本（research/2026-09-13_phase-migration-rolling/
trend_phase_rolling.py）同一口径：instrument_metadata 中 asset_type 为
stock/etf 的标的（默认只补启用中的）。

用法：
    sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py            # 全池整段重建
    sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py --symbols 510300.SS,600036.SS
    sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py --include-disabled
    sudo -u trendquant .venv/bin/python scripts/backfill_rolling_trend.py --symbols 510300.SS --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import _common  # noqa: E402  # .env 加载（锚定项目根）+ DB_PATH

import pandas as pd  # noqa: E402

from core.rolling_bars import (  # noqa: E402
    rolling_period_trend_series,
    rolling_trend_cfg,
)
from data.storage.db import get_db, init_db  # noqa: E402

CFG_W = rolling_trend_cfg("1w")
CFG_M = rolling_trend_cfg("1M")


def _rolling_trend_frame(daily: "pd.DataFrame") -> "pd.DataFrame":
    """qfq 日K → (time, w_trend, m_trend) 帧（与 DataService._rolling_trend_frame 同口径）。"""
    w = rolling_period_trend_series(daily, CFG_W, period="1w")
    m = rolling_period_trend_series(daily, CFG_M, period="1M")
    return pd.DataFrame({"w_trend": w, "m_trend": m}).reset_index()


def _pool_symbols(include_disabled: bool) -> list[str]:
    """instrument_metadata 中 asset_type 为 stock/etf 的标的（与研究脚本同口径）。"""
    instruments = get_db().list_instrument_metadata()
    symbols: list[str] = []
    seen: set[str] = set()
    for item in instruments:
        asset = str(item.get("asset_type") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        if asset not in ("stock", "etf") or not symbol or symbol in seen:
            continue
        if not include_disabled and not bool(item.get("enabled", True)):
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="滚动周/月趋势值回填（幂等，可重复执行）")
    parser.add_argument("--symbols", default=None, help="限定标的，逗号分隔（缺省=启用中的 stock+etf 全池）")
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="连 enabled=0 的标的也一并补齐（默认只补启用中的）",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="每批载入日K 的标的数")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只计算与打印，不写库（验证用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = _common.setup_script_logging("backfill_rolling_trend")
    init_db(_common.DB_PATH)
    db = get_db()

    if args.symbols:
        symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    else:
        symbols = _pool_symbols(bool(args.include_disabled))
    if not symbols:
        print("没有需要回填的标的")
        return 2
    print(f"滚动周/月趋势值回填：{len(symbols)} 只标的"
          f"{'（dry-run，不写库）' if args.dry_run else ''}")

    started = time.monotonic()
    batch_size = max(1, int(args.batch_size))
    total_written = 0
    done_symbols = 0
    failures: list[str] = []
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i : i + batch_size]
        frames = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        for symbol in chunk:
            daily = frames.get(symbol)
            if daily is None or daily.empty:
                continue
            try:
                frame = _rolling_trend_frame(daily)
                if args.dry_run:
                    rows = len(frame.dropna(subset=["w_trend", "m_trend"], how="all"))
                else:
                    # 回填按整段重建口径：先删后写，幂等且能清掉历史脏行
                    rows = db.replace_rolling_trend(symbol, frame)
                total_written += rows
                done_symbols += 1
            except Exception:  # 单标的失败不拖垮整批，结尾汇总失败列表
                failures.append(symbol)
                logger.exception("rolling trend backfill failed for %s", symbol)
        print(f"  {min(i + batch_size, len(symbols))}/{len(symbols)}，"
              f"累计写入 {total_written:,} 行", flush=True)

    elapsed = time.monotonic() - started
    logger.info(
        "rolling trend backfill: %d symbols, rows=%d, elapsed=%.1fs, failures=%s",
        done_symbols, total_written, elapsed, failures,
    )
    print(f"完成：{done_symbols} 只标的；写入 {total_written:,} 行；耗时 {elapsed:.1f}s")
    if failures:
        print(f"失败 {len(failures)} 只：{','.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
