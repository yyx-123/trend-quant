"""tushare 申万行业分类全量同步（季度临时账号窗口内运行）。

方案文档：docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md §5

index_classify（2000 积分）拿 SW2021 一级列表 → 逐一级 index_member_all
（2000 积分，单次最大 2000 行，按一级拆分天然分页）拿官方全量成分，
以 source=tushare_sw2021 写入 stock_industry（覆盖 tickflow 行，不动 manual 行；
只增/改、从不删行，退市股行业信息保留）。

同步后输出「归属变更清单」（官方最新归属 vs 在管标的当前类目，方案 §8）
并执行待分类回补（方案 §5）。

用法（项目根目录，先 pip install tushare）：
    set TUSHARE_TOKEN=xxx
    .venv/Scripts/python scripts/sync_sw_tushare.py              # 全量同步
    .venv/Scripts/python scripts/sync_sw_tushare.py --dry-run    # 只拉取统计，不写库
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data.storage.db import init_db, record_job_run_safely  # noqa: E402
from services.stock_industry import (  # noqa: E402
    SOURCE_TUSHARE,
    STOCK_L1,
    UNCLASSIFIED_L2,
    add_missing_tree_branches,
    is_a_share,
    normalize_industry_name,
    reclassify_pending_stocks,
    tickflow_symbol_to_project,
)
from tushare_common import call_with_retry, get_pro_api  # noqa: E402

DB_PATH = Path("data/trend_quant.db")
_JOB_TYPE = "stock_industry_sync_tushare"


def fetch_sw_rows(pro, interval: float = 0.4) -> tuple[list[dict], list[str]]:
    """拉取 SW2021 全量成分，返回 (stock_industry 行列表, 警告列表)。"""
    warnings: list[str] = []
    classify = call_with_retry(pro.index_classify, level="L1", src="SW2021")
    l1_codes = list(classify["index_code"])
    if not l1_codes:
        raise RuntimeError("index_classify 返回空——检查 token 权限（需 2000 积分）")

    rows: dict[str, dict] = {}
    for idx, l1_code in enumerate(l1_codes):
        if idx:
            time.sleep(interval)
        members = call_with_retry(pro.index_member_all, l1_code=l1_code, is_new="Y")
        if len(members) >= 2000:
            warnings.append(f"{l1_code} 返回满 2000 行，可能被截断")
        # 同一只股票可能有多行（历史调仓记录全标 is_new='Y'，实测电子 745 行/530 只）
        # ——按 in_date 升序遍历，字典覆盖后保留最新一次行业归属。
        if "in_date" in members.columns:
            members = members.sort_values("in_date", kind="stable")
        for rec in members.to_dict("records"):
            symbol = tickflow_symbol_to_project(rec.get("ts_code"))
            if not is_a_share(symbol):
                continue
            rows[symbol] = {
                "symbol": symbol,
                "sw_l1_name": normalize_industry_name(rec.get("l1_name")),
                "sw_l2_name": normalize_industry_name(rec.get("l2_name")),
                "sw_l3_name": str(rec.get("l3_name") or "").strip(),
                "sw_l3_code": str(rec.get("l3_code") or "").strip(),
            }
    return list(rows.values()), warnings


def industry_change_report(db) -> list[dict]:
    """归属变更清单：stock_industry（官方最新）vs instrument_metadata（当前类目）。

    只报告、不自动改（方案 §8 保守原则）；待分类标的不在报告内（走回补）。
    """
    industry = {row["symbol"]: row for row in db.list_stock_industry()}
    changes: list[dict] = []
    for item in db.list_instrument_metadata():
        if not item.get("enabled"):
            continue
        if str(item.get("category_l1") or "").strip() != STOCK_L1:
            continue
        current_l2 = str(item.get("category_l2") or "").strip()
        if current_l2 == UNCLASSIFIED_L2:
            continue
        row = industry.get(str(item.get("symbol") or "").strip().upper())
        if row is None:
            continue
        if row["sw_l1_name"] != current_l2 or row["sw_l2_name"] != str(
            item.get("category_l3") or ""
        ).strip():
            changes.append(
                {
                    "symbol": item["symbol"],
                    "name": str(item.get("name") or "").strip(),
                    "current": f"{current_l2}-{item.get('category_l3')}",
                    "official": f"{row['sw_l1_name']}-{row['sw_l2_name']}",
                    "source": row["source"],
                }
            )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description="tushare 申万行业分类全量同步")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写库")
    parser.add_argument("--no-reclassify", action="store_true", help="跳过待分类回补")
    parser.add_argument("--interval", type=float, default=0.4, help="调用间隔秒数")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[sync-sw] 找不到数据库 {args.db}", file=sys.stderr)
        return 1

    pro = get_pro_api()
    db = init_db(args.db)

    rows, warnings = fetch_sw_rows(pro, interval=args.interval)
    for w in warnings:
        print(f"[sync-sw] 警告: {w}")
    print(f"[sync-sw] 拉取 {len(rows)} 只股票行业归属")

    if args.dry_run:
        print("[sync-sw] dry-run，未写库。")
        return 0

    written = db.upsert_stock_industry(rows, SOURCE_TUSHARE)
    print(f"[sync-sw] 写入={written} 被 manual 挡下={len(rows) - written}")

    # 用官方全量补齐树分支（tickflow universe 名录可能缺个别 L2 叶子，如 计算机-IT服务）
    added_branches = add_missing_tree_branches(db, rows)
    if added_branches:
        print(f"[sync-sw] 申万树补充分支 {len(added_branches)} 个: {added_branches}")

    changes = industry_change_report(db)
    print(f"[sync-sw] 归属变更清单 {len(changes)} 只（仅报告，不自动改）:")
    for c in changes:
        print(f"  {c['symbol']} {c['name']}: {c['current']} → {c['official']} ({c['source']})")

    reclassify = None
    if not args.no_reclassify:
        reclassify = reclassify_pending_stocks(db=db)
        moved = reclassify["moved"]
        print(
            f"[sync-sw] 待分类回补：待处理={reclassify['pending']} 移动={len(moved)} "
            f"树未就绪延迟={reclassify['deferred']} 仍未分类={len(reclassify['still_unclassified'])}"
        )
        for item in moved:
            print(f"  {item['symbol']} {item['name']} → {item['to']} ({item['source']})")

    record_job_run_safely(
        _JOB_TYPE,
        {
            "rows": len(rows),
            "written": written,
            "warnings": warnings,
            "industry_changes": changes,
            "reclassify": reclassify,
        },
        status="success",
    )
    print("[sync-sw] 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
