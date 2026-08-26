"""生产环境数据迁移：raw 真源 + 除权因子本地物化 qfq（2026-08 复权事故修复）。

背景：历史 qfq 数据由 vendor 等差前复权（forward_additive）直接落库，对早期
低价 + 高累计分红的标的历史价格被减穿零（40 个标的负价、~250 个标的收益率
失真）。本脚本把存储体系迁移为：

    raw（不复权，append-only 真源） + ex_factors（除权因子） → 本地物化 qfq（等比）

做的事（对每个标的）：
    1. 补全 raw 历史（增量 tail 补齐；raw 历史行不会被 vendor 回溯改写，可信）；
    2. 同步除权因子（批量，~N/100 次请求）；
    3. 由 raw + 因子本地物化等比 qfq，全量重写 qfq 表（清除旧的等差脏数据）；
    4. 校验 qfq 表不再存在非正价格；
    5. （可选）抽样与 vendor forward 逐行对比验证；
    6. （可选）重建指标/趋势缓存。

用法（生产环境，项目根目录）：
    .venv/Scripts/python.exe scripts/migrate_raw_qfq.py                 # 全量迁移
    .venv/Scripts/python.exe scripts/migrate_raw_qfq.py --symbols 300274.SZ,600519.SS --verify-vendor
    .venv/Scripts/python.exe scripts/migrate_raw_qfq.py --resume        # 中断后续跑
    .venv/Scripts/python.exe scripts/migrate_raw_qfq.py --dry-run       # 只看不改

迁移完成后请重跑批量回测（旧的批次结果是脏数据口径下的产物）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import _common  # .env 加载（锚定项目根）+ DB_PATH（P2-13）

from data.service import DataService
from data.storage.db import init_db

CHECKPOINT_PATH = Path("data/migrate_raw_qfq.checkpoint.json")


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "failed": {}, "started": datetime.now().isoformat()}


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def qfq_nonpositive_symbols(db) -> list[str]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM market_data_qfq WHERE close <= 0 OR open <= 0"
        ).fetchall()
    return sorted(r["symbol"] for r in rows)


def verify_against_vendor(service: DataService, symbols: list[str], start: date, end: date) -> dict:
    """抽样：本地物化 qfq vs vendor forward，逐行对比 close。"""
    import pandas as pd

    report = {}
    for symbol in symbols:
        try:
            local = service.market_store.load_history(symbol)
            vendor = service.fetch_daily_history(symbol, start, end, adjust="qfq")
            if local.empty or vendor.empty:
                report[symbol] = "skipped (empty)"
                continue
            local = local.copy()
            vendor = vendor.copy()
            local["d"] = pd.to_datetime(local["time"]).dt.strftime("%Y-%m-%d")
            vendor["d"] = pd.to_datetime(vendor["time"]).dt.strftime("%Y-%m-%d")
            merged = local.merge(vendor, on="d", suffixes=("_local", "_vendor"))
            rel = (merged["close_local"] - merged["close_vendor"]).abs() / merged["close_vendor"].replace(0, 1)
            report[symbol] = f"rows={len(merged)} max_rel_err={float(rel.max()):.2e}"
        except Exception as exc:
            report[symbol] = f"error: {exc}"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="raw 真源 + 因子物化 qfq 数据迁移")
    parser.add_argument("--symbols", default="", help="逗号分隔标的列表；默认 qfq 表全部标的")
    parser.add_argument("--start", default="1990-01-01", help="raw 历史起点（默认 1990-01-01）")
    parser.add_argument("--end", default=date.today().isoformat(), help="终点（默认今天）")
    parser.add_argument("--chunk", type=int, default=100, help="每批标的数（默认 100，对应批量接口上限）")
    parser.add_argument("--resume", action="store_true", help="从断点续跑")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写数据")
    parser.add_argument("--verify-vendor", action="store_true", help="迁移后抽样与 vendor forward 对比")
    parser.add_argument("--no-rebuild", action="store_true", help="跳过指标/趋势缓存重建")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    db = init_db(_common.DB_PATH)
    service = DataService()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(set(db.list_market_symbols(price_mode="qfq")) | set(db.list_market_symbols(price_mode="raw")))
    print(f"[migrate] 标的数: {len(symbols)}, 区间: {start} ~ {end}")

    state = load_checkpoint() if args.resume else {"done": [], "failed": {}, "started": datetime.now().isoformat()}
    done = set(state["done"])
    todo = [s for s in symbols if s not in done]
    print(f"[migrate] 待处理: {len(todo)}（已完成 {len(done)}）")
    if args.dry_run:
        print("[migrate] dry-run，退出。")
        return 0

    chunks = [todo[i : i + args.chunk] for i in range(0, len(todo), args.chunk)]
    for idx, chunk in enumerate(chunks, start=1):
        print(f"[migrate] chunk {idx}/{len(chunks)} ({len(chunk)} symbols) ...", flush=True)
        items = [{"symbol": s, "start_date": start} for s in chunk]

        def progress(event: dict) -> None:
            if event.get("event") == "item_done":
                print(f"  [{event.get('finished')}/{event.get('total')}] {event.get('symbol', '')}", flush=True)

        results = service.backfill_daily_histories(items, end, progress_callback=progress)
        for payload in results:
            symbol = payload.get("symbol") or (payload.get("result") or {}).get("symbol")
            if payload.get("ok"):
                state["done"].append(symbol)
                state["failed"].pop(symbol, None)
            else:
                state["failed"][symbol] = payload.get("error", "unknown")
        save_checkpoint(state)
        time.sleep(1)  # chunk 间喘息，避免触发限额

    print(f"[migrate] 行情迁移完成: ok={len(state['done'])} failed={len(state['failed'])}")
    if state["failed"]:
        print(f"[migrate] 失败明细: {state['failed']}")

    bad = qfq_nonpositive_symbols(db)
    print(f"[migrate] qfq 非正价格标的数: {len(bad)} {bad[:10]}")

    if args.verify_vendor and state["done"]:
        sample = state["done"][:5]
        print(f"[migrate] vendor forward 抽样对比: {verify_against_vendor(service, sample, start, end)}")

    if not args.no_rebuild and state["done"]:
        from core.strategy_config import get_strategy_config
        from services.indicator_builder import rebuild_all

        print("[migrate] 重建指标/趋势缓存 ...", flush=True)
        result = rebuild_all(symbols=sorted(state["done"]), trend_cfg=get_strategy_config(), db=db)
        print(f"[migrate] 缓存重建: {result}")

    print("[migrate] 完成。请重跑批量回测（旧批次结果是脏数据口径的产物）。")
    return 0 if not state["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
