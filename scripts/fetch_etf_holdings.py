"""ETF 前十大重仓股季度快照（tushare 临时账号窗口内运行）。

方案文档：
- docs/stock-industry-etf-holdings/2026-08-24-stock-industry-etf-holdings-plan.md
- docs/etf-weighted-stocks/2026-07-30-etf-weighted-stocks-plan.md §5（本脚本即其落地）

tushare fund_portfolio（5000 积分，季度更新，季报口径天然即前十大重仓）。
应用运行时对 tushare 零依赖；快照按 (etf_symbol, period) 幂等 upsert，
中断后重跑同一条命令即可断点续传。

用法（项目根目录，先 pip install tushare）：
    set TUSHARE_TOKEN=xxx
    .venv/Scripts/python scripts/fetch_etf_holdings.py                          # 全量
    .venv/Scripts/python scripts/fetch_etf_holdings.py --symbols 510300.SS --dry-run
    .venv/Scripts/python scripts/fetch_etf_holdings.py --period 20260630 --force
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
    is_a_share,
    project_symbol_to_tushare,
    tickflow_symbol_to_project,
)
from tushare_common import call_with_retry, get_pro_api, prev_period, target_period  # noqa: E402

DB_PATH = Path("data/trend_quant.db")
_JOB_TYPE = "etf_constituents_fetch"
_MAX_CONSECUTIVE_ERRORS = 5


def fetch_top10(pro, etf_symbol: str, period: str) -> tuple[list[dict], str | None]:
    """fund_portfolio → 前十大重仓股（项目代码格式）。返回 (rows, 实际期次|None)。

    目标期次为空时自动回退上一季度重试一次；仍为空返回 ([], None)。
    """
    ts_code = project_symbol_to_tushare(etf_symbol)
    for candidate in (period, prev_period(period)):
        df = call_with_retry(pro.fund_portfolio, ts_code=ts_code, period=candidate)
        if df is None or df.empty:
            continue
        records = []
        for rec in df.to_dict("records"):
            stock = tickflow_symbol_to_project(rec.get("symbol"))
            if not is_a_share(stock):
                continue  # QDII 港股/美股、北交所丢弃
            records.append(
                {
                    "stock_symbol": stock,
                    "weight": rec.get("stk_mkv_ratio"),
                    "ann_date": str(rec.get("ann_date") or "").strip(),
                }
            )
        records.sort(key=lambda r: (r["weight"] is None, -(r["weight"] or 0)))
        rows = [
            {**r, "rank": i + 1} for i, r in enumerate(records[:10])
        ]
        if rows:
            return rows, candidate
    return [], None


def make_tickflow_client():
    """tickflow 客户端（补股票名称用，fund_portfolio 只给代码）。缺 key 时返回 None。"""
    import os

    api_key = str(os.getenv("TICKFLOW_API_KEY", "") or "").strip()
    if not api_key:
        return None
    try:
        from tickflow import TickFlow
    except ImportError:
        return None
    return TickFlow(api_key=api_key, base_url="https://api.tickflow.org")


def fill_stock_names(rows: list[dict], client) -> None:
    """批量补股票名称（≤10 只一次请求）；失败仅告警，不阻断落库。"""
    if client is None or not rows:
        return
    try:
        symbols = [project_symbol_to_tushare(r["stock_symbol"]) for r in rows]
        insts = client.instruments.batch(symbols)
        names = {
            str(i.get("symbol") or "").upper(): str(i.get("name") or "").strip() for i in insts
        }
        for r in rows:
            r["stock_name"] = names.get(project_symbol_to_tushare(r["stock_symbol"]), "")
    except Exception as exc:
        print(f"[fetch] 名称补全失败（不影响落库）: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 前十大重仓股季度快照（tushare）")
    parser.add_argument("--period", default=None, help="报告期 YYYYMMDD，默认自动推算")
    parser.add_argument("--force", action="store_true", help="忽略已有期次数据，全量重抓")
    parser.add_argument("--symbols", default=None, help="只抓指定 ETF（逗号分隔，调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不入库")
    parser.add_argument("--interval", type=float, default=0.4, help="调用间隔秒数")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[fetch] 找不到数据库 {args.db}", file=sys.stderr)
        return 1

    db = init_db(args.db)
    pro = get_pro_api()
    tf_client = make_tickflow_client()
    if tf_client is None:
        print("[fetch] 警告：无 TICKFLOW_API_KEY，重仓股名称将为空（不影响导入流程）")
    period = str(args.period or target_period())
    print(f"[fetch] 目标期次: {period}")

    etfs = [
        item
        for item in db.list_instrument_metadata()
        if item.get("enabled") and str(item.get("asset_type") or "") == "etf"
    ]
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        etfs = [item for item in etfs if item["symbol"] in wanted]
        missing = wanted - {item["symbol"] for item in etfs}
        if missing:
            print(f"[fetch] 警告：以下代码不是在管 ETF: {sorted(missing)}")
    print(f"[fetch] 在管 ETF {len(etfs)} 只")

    summary = {"period": period, "success": 0, "fallback": 0, "no_data": [], "skipped": 0, "failed": []}
    consecutive_errors = 0
    for idx, item in enumerate(etfs):
        etf = item["symbol"]
        if not args.force and db.has_etf_constituents_for_period(etf, period):
            summary["skipped"] += 1
            continue
        if idx:
            time.sleep(args.interval)
        try:
            rows, actual_period = fetch_top10(pro, etf, period)
            consecutive_errors = 0
        except Exception as exc:  # 单只失败仅记录，连续失败中止（账号/网络问题）
            consecutive_errors += 1
            summary["failed"].append({"etf": etf, "error": str(exc)[:200]})
            print(f"[fetch] {etf} 失败: {exc}")
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                print(f"[fetch] 连续 {consecutive_errors} 次失败，中止（检查账号/网络）")
                break
            continue

        if not rows:
            summary["no_data"].append(etf)  # 债券/货币/QDII 等，不算失败
            continue
        fill_stock_names(rows, tf_client)
        if actual_period != period:
            summary["fallback"] += 1
        print(f"[fetch] {etf} {item.get('name') or ''}: 期次 {actual_period}，前十 {len(rows)} 只")
        if not args.dry_run:
            # period 列记录数据的真实报告期（回退期次就如实记回退期次），
            # UI 新鲜度判断才不会被高估；代价是同窗口重跑会重抓回退标的，可接受。
            db.save_etf_constituents(etf, rows, actual_period)
        summary["success"] += 1

    print(
        f"[fetch] 汇总：成功={summary['success']}（回退期次 {summary['fallback']}） "
        f"跳过={summary['skipped']} 无数据={len(summary['no_data'])} 失败={len(summary['failed'])}"
    )
    if not args.dry_run:
        record_job_run_safely(
            _JOB_TYPE,
            {**summary, "no_data": summary["no_data"], "failed": summary["failed"]},
            status="success" if not summary["failed"] else "partial",
        )
    print("[fetch] dry-run，未写库。" if args.dry_run else "[fetch] 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
