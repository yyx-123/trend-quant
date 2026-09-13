"""拟合周/月K（market_data_qfq_*_fitted）全量回填脚本（幂等，可重复执行）。

从 market_data_qfq（日K 前复权）本地聚合出「每个交易日的在途周/月 bar」，
写入两张拟合表。用途：

- 新部署/新功能上线时首次把拟合表建满；
- 拟合表数据异常后的修复性重建（``--symbols`` 可限定标的）。

日常维护不需要本脚本：日更任务（ensure_daily_history）会为更新的标的
增量维护拟合行、除权因子变化时整段重建（见 docs/26-09-13-fitted-period-bars/）。

用法：
    .venv/bin/python scripts/backfill_fitted_period_bars.py            # 全池整段重建
    .venv/bin/python scripts/backfill_fitted_period_bars.py --symbols 510300.SS,600036.SS
    .venv/bin/python scripts/backfill_fitted_period_bars.py --include-disabled
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import _common  # noqa: E402  # .env 加载（锚定项目根）+ DB_PATH

from core.bars import fitted_period_rows  # noqa: E402
from core.benchmarks import benchmark_market_symbols  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402

PERIODS = ("1w", "1M")


def _pool_symbols(include_disabled: bool) -> list[str]:
    """启用中的标的 + 基准指数（与日更任务同一来源，口径保持一致）。"""
    instruments = get_db().list_instrument_metadata()
    symbols: list[str] = []
    seen: set[str] = set()
    for item in instruments:
        if not include_disabled and not bool(item.get("enabled", True)):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    for raw_symbol in benchmark_market_symbols():
        symbol = str(raw_symbol or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拟合周/月K 回填（幂等，可重复执行）")
    parser.add_argument("--symbols", default=None, help="限定标的，逗号分隔（缺省=启用中的全池）")
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="连 enabled=0 的标的也一并补齐（默认只补启用中的）",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="每批写入的标的数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = _common.setup_script_logging("backfill_fitted_period_bars")
    init_db(_common.DB_PATH)
    db = get_db()

    if args.symbols:
        symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    else:
        symbols = _pool_symbols(bool(args.include_disabled))
    if not symbols:
        print("没有需要回填的标的")
        return 2
    print(f"拟合周/月K 回填：{len(symbols)} 只标的，周期 {','.join(PERIODS)}")

    batch_size = max(1, int(args.batch_size))
    total_written = {period: 0 for period in PERIODS}
    done_symbols = 0
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i : i + batch_size]
        frames = db.load_market_data_many(chunk, price_mode="qfq", period="1d")
        for period in PERIODS:
            items = []
            for symbol, daily in frames.items():
                if daily is None or daily.empty:
                    continue
                items.append((symbol, fitted_period_rows(daily, period)))
            # 回填按整段重建口径：先删后写（本批一次事务 per 标的由
            # replace 保证；批量 upsert 走 save_many 也行，但 replace 能清掉
            # 历史遗留的脏行，回填用它更干净——逐标的 replace 一次连接都够快）
            for symbol, fitted in items:
                written = db.replace_fitted_period_bars(symbol, fitted, period)
                total_written[period] += written
        done_symbols += len(frames)
        print(f"  {min(i + batch_size, len(symbols))}/{len(symbols)}，"
              f"累计写入 周 {total_written['1w']:,} / 月 {total_written['1M']:,}", flush=True)

    logger.info(
        "fitted period bars backfill: %d symbols, weekly=%d, monthly=%d",
        done_symbols, total_written["1w"], total_written["1M"],
    )
    print(f"完成：{done_symbols} 只标的；写入 周 {total_written['1w']:,} 行 / "
          f"月 {total_written['1M']:,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
