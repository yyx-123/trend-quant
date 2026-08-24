"""一键补齐全部 ETF 前十大重仓股到标的池（极低频运维脚本，无页面入口）。

把 etf_constituents 快照里所有「当前前十大重仓股」中尚未管理的股票批量
入池：逐只 add_constituent_stock（自动申万归类，与页面导入同一入口），
然后一次性批量回补日 K 并重建指标。已在管理的自动跳过，幂等可重跑。

用法（项目根目录）：
    .venv/Scripts/python scripts/import_all_etf_constituents.py            # 全量导入
    .venv/Scripts/python scripts/import_all_etf_constituents.py --dry-run  # 只统计不写库
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data.service import DataService  # noqa: E402
from data.storage.db import init_db, record_job_run_safely  # noqa: E402
from services.indicator_builder import rebuild_after_backfill  # noqa: E402
from services.instrument_admin import _known_managed_symbols, add_constituent_stock  # noqa: E402

DB_PATH = Path("data/trend_quant.db")
_JOB_TYPE = "etf_constituents_import_all"


def main() -> int:
    parser = argparse.ArgumentParser(description="全部 ETF 前十大重仓股一键入池")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库不回补")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[import-all] 找不到数据库 {args.db}", file=sys.stderr)
        return 1

    db = init_db(args.db)
    rows = db.list_all_current_etf_constituents()
    if not rows:
        print("[import-all] etf_constituents 无快照数据，请先运行 scripts/fetch_etf_holdings.py")
        return 1

    # 去重（一只股票是多只 ETF 的重仓），保留首次出现的名称
    stocks: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("stock_symbol") or "").strip().upper()
        if symbol and symbol not in stocks:
            stocks[symbol] = str(row.get("stock_name") or "").strip()
    etf_count = len({str(r.get("etf_symbol") or "") for r in rows})
    print(f"[import-all] {etf_count} 只 ETF 的快照，去重后 {len(stocks)} 只股票")

    known = _known_managed_symbols()
    todo = {s: n for s, n in stocks.items() if s not in known}
    print(f"[import-all] 已在管理 {len(stocks) - len(todo)} 只，待导入 {len(todo)} 只")
    if args.dry_run:
        from services.stock_industry import resolve_category

        hit = sum(1 for s in todo if resolve_category(s, db=db)["hit"])
        print(f"[import-all] 其中行业命中 {hit} 只，待分类 {len(todo) - hit} 只。dry-run，未写库。")
        return 0
    if not todo:
        print("[import-all] 无待导入标的，完成。")
        return 0

    added: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for idx, (symbol, name) in enumerate(todo.items(), start=1):
        outcome = add_constituent_stock(symbol, name, known_symbols=known)
        if outcome["status"] == "added":
            known.add(symbol)
            added.append(outcome)
        elif outcome["status"] == "skipped":
            skipped.append(outcome)
        else:
            failed.append(outcome)
        if idx % 50 == 0 or idx == len(todo):
            print(f"[import-all] 写入进度 {idx}/{len(todo)}")

    print(f"[import-all] 新增 {len(added)} 只（待分类 {sum(1 for a in added if not a['hit'])} 只），"
          f"跳过 {len(skipped)} 只，失败 {len(failed)} 只")

    backfill_updated = 0
    backfill_failed: list[str] = []
    if added:
        print(f"[import-all] 开始批量回补 {len(added)} 只标的历史行情（2020-01-01 起）...")
        service = DataService()
        try:
            payloads = service.backfill_daily_histories(
                items=[{"symbol": a["symbol"], "start_date": date(2020, 1, 1)} for a in added],
                end_date=date.today(),
                adjust="qfq",
                max_retries=3,
                batch_size=100,
                request_interval_seconds=2.0,
                retry_delay_seconds=2.0,
            )
        finally:
            service.close()
        updated_symbols: list[str] = []
        for payload in payloads:
            if payload.get("ok") and str((payload.get("result") or {}).get("status") or "") not in (
                "error",
                "no_data",
            ):
                backfill_updated += 1
                updated_symbols.append(
                    str((payload.get("result") or {}).get("symbol") or "").strip().upper()
                )
            else:
                backfill_failed.append(
                    str((payload.get("result") or {}).get("symbol") or payload.get("symbol") or "")
                )
        print(f"[import-all] 行情回补成功 {backfill_updated} 只，失败 {len(backfill_failed)} 只 {backfill_failed or ''}")
        if updated_symbols:
            print("[import-all] 重建指标与趋势缓存...")
            rebuild_after_backfill(updated_symbols)

    record_job_run_safely(
        _JOB_TYPE,
        {
            "etfs": etf_count,
            "distinct_stocks": len(stocks),
            "added": [a["symbol"] for a in added],
            "added_unclassified": [a["symbol"] for a in added if not a["hit"]],
            "skipped": len(skipped),
            "failed": failed,
            "backfill_updated": backfill_updated,
            "backfill_failed": backfill_failed,
        },
        status="success" if not failed else "partial",
    )
    print("[import-all] 完成。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
