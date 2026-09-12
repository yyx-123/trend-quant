"""周K/月K 全量补数 / 增量补齐脚本（可重复执行）。

用途：本地或服务器首次铺开周/月K 数据，以及后续手工补跑（日常增量由
16:30 日更任务自带，本脚本只在下列场景需要）：

- 新部署的机器首次把周/月K 全部拉下来（每标的整段历史一次请求）；
- 日更连续失败/停机一段时间后的补齐（幂等，已最新的标的零写入）；
- 只针对少量标的排查（``--symbols 510300.SS,600036.SS``）。

口径（详见 docs/26-09-12-周月K/）：
- 只落已收盘周期，进行中的周/月 bar 不入库；
- raw 与 qfq 两张表：qfq 直接取 vendor 前复权（周/月 bar 可能横跨除权日，
  无法由 raw 周期 bar 本地物化），除权因子变化时该标的 qfq 整段重取；
- 首段起点默认对齐该标的日K最早日（与已有日K覆盖一致），``--start`` 可覆盖。

用法：
    .venv/bin/python scripts/backfill_period_history.py                 # 全池 日更式增量
    .venv/bin/python scripts/backfill_period_history.py --start 1990-01-01
    .venv/bin/python scripts/backfill_period_history.py --periods 1w
    .venv/bin/python scripts/backfill_period_history.py --symbols 510300.SS,159915.SZ
    .venv/bin/python scripts/backfill_period_history.py --include-disabled
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))

import _common  # noqa: E402  # .env 加载（锚定项目根）+ DB_PATH

from core.bars import SUPPORTED_PERIODS  # noqa: E402
from core.benchmarks import benchmark_market_symbols  # noqa: E402
from core.calendar import market_now  # noqa: E402
from data.service import DataService  # noqa: E402
from data.storage.db import get_db, init_db  # noqa: E402


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
    parser = argparse.ArgumentParser(description="周K/月K 补数（幂等，可重复执行）")
    parser.add_argument(
        "--periods",
        default="1w,1M",
        help=f"周期，逗号分隔（可选 {'/'.join(SUPPORTED_PERIODS)}；日K不在本脚本范围）",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="首段抓取起点 YYYY-MM-DD（仅在标的尚无周/月K时生效；缺省对齐该标的日K最早日）",
    )
    parser.add_argument("--symbols", default=None, help="限定标的，逗号分隔（缺省=启用中的全池）")
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="连 enabled=0 的标的也一并补齐（默认只补启用中的）",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="每批标的数（vendor 上限 100）")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数")
    parser.add_argument("--retry-interval", type=float, default=10.0, help="重试间隔秒")
    parser.add_argument("--end", default=None, help="截止日 YYYY-MM-DD（缺省=今天）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = _common.setup_script_logging("backfill_period_history")
    init_db(_common.DB_PATH)  # 与应用启动同一路径（锚定项目根）

    periods = tuple(p.strip() for p in str(args.periods).split(",") if p.strip())
    if args.symbols:
        symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    else:
        symbols = _pool_symbols(bool(args.include_disabled))
    if not symbols:
        print("没有需要补齐的标的")
        return 2
    start_date = date.fromisoformat(args.start) if args.start else None
    end_date = date.fromisoformat(args.end) if args.end else market_now().date()

    print(
        f"周/月K 补数：{len(symbols)} 只标的，周期 {','.join(periods)}，"
        f"截止 {end_date.isoformat()}，起点 {start_date.isoformat() if start_date else '对齐各标的日K最早日'}"
    )
    service = DataService()
    try:
        payload = service.update_pool_periods(
            symbols,
            periods,
            start_date=start_date,
            end_date=end_date,
            batch_size=max(1, min(int(args.batch_size), 100)),
            max_retries=max(0, int(args.retries)),
            retry_interval_seconds=max(0.0, float(args.retry_interval)),
            progress_callback=lambda event: logger.info(
                "period %s", {k: v for k, v in event.items() if k != "results"}
            ),
        )
    finally:
        service.close()

    for period, info in sorted((payload.get("periods") or {}).items()):
        print(
            f"[{period}] 计划 {info['planned']} / 更新 {info['updated']} / 已最新 {info['up_to_date']}"
            f" / 失败 {info['failed']}；写入 {info['rows_written']} 行，"
            f"丢弃未收盘 bar {info['dropped_open_period']} 根，整段重取 {info['full_refetch']} 只"
        )
        coverage = info.get("coverage") or {}
        print(f"[{period}] 覆盖区间：{coverage.get('start')} ~ {coverage.get('end')}")
        if info.get("failed_symbols"):
            print(f"[{period}] 失败标的（前 20）：{info['failed_symbols'][:20]}")
            for symbol, message in (info.get("errors") or {}).items():
                print(f"    {symbol}: {message}")
    print(
        f"完成：{payload.get('success', 0)} 只成功 / {payload.get('failed', 0)} 只失败"
        f"（状态 {payload.get('status')}）"
    )
    # 逐标的明细落脚本日志，便于事后核对
    for period, info in (payload.get("periods") or {}).items():
        for row in info.get("results") or []:
            logger.info(
                "period=%s %s fetch_start=%s full=%s raw=%s qfq=%s local=%s~%s",
                period,
                row["symbol"],
                row["fetch_start"],
                row["full"],
                row["rows"].get("raw"),
                row["rows"].get("qfq"),
                row["local_start"],
                row["local_end"],
            )
    return 0 if not payload.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
