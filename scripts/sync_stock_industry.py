"""从 TickFlow universes 同步申万行业分类到 stock_industry 表（免费，starter 档）。

方案文档：docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md §5

用法（项目根目录）：
    .venv/Scripts/python scripts/sync_stock_industry.py              # 全量同步 + 待分类回补
    .venv/Scripts/python scripts/sync_stock_industry.py --no-reclassify
    .venv/Scripts/python scripts/sync_stock_industry.py --dry-run    # 只拉取统计，不写库

调度器月度任务调用的是同一个 service 函数（services.stock_industry.sync_industry_from_tickflow）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _common  # .env 加载 + DB_PATH + TickFlow 构造（P2-13）

logger = _common.setup_script_logging(__name__)

from data.storage.db import init_db
from services.stock_industry import (
    record_industry_sync_job,
    sync_industry_from_tickflow,
)

DB_PATH = _common.DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="TickFlow 申万行业分类同步")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写库")
    parser.add_argument("--no-reclassify", action="store_true", help="跳过待分类回补")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    if not Path(args.db).exists():
        logger.info(f"[sync] 找不到数据库 {args.db}")
        return 1

    db = init_db(args.db)

    summary = sync_industry_from_tickflow(
        db=db, reclassify=not args.no_reclassify, write=not args.dry_run
    )
    if args.dry_run:
        logger.info(
            f"[sync] dry-run：universes={summary['universes']} 股票行={summary['rows']}，未写库。"
        )
        return 0
    record_industry_sync_job("stock_industry_sync_tickflow", summary)

    logger.info(
        f"[sync] universes={summary['universes']} 股票行={summary['rows']} "
        f"写入={summary['written']} 被高优先级挡下={summary['skipped_by_priority']}"
    )
    reclassify = summary.get("reclassify")
    if reclassify is not None:
        moved = reclassify["moved"]
        logger.info(
            f"[sync] 待分类回补：待处理={reclassify['pending']} 移动={len(moved)} "
            f"树未就绪延迟={reclassify['deferred']} 仍未分类={len(reclassify['still_unclassified'])}"
        )
        for item in moved:
            logger.info(f"  {item['symbol']} {item['name']} → {item['to']} ({item['source']})")
        if reclassify["still_unclassified"]:
            logger.info(f"  仍未分类: {reclassify['still_unclassified']}")
    logger.info("[sync] 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
