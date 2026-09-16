"""全市场近 20 日日均成交额：为流动性筛选提供不受盘中时点影响的口径。

产出 ``data/liquidity_20d.csv``：symbol + 近 N 个交易日日均成交额(亿) + 有效K线数。

为什么不用盘中报价里的 amount：2026-09-16 扫描时点为 14:15，当日成交额只是
半个交易日，会让一批"全天刚过 1 亿"的票被误杀。20 日均额同时对单日异动免疫。

数据源：复用 ``data.provider_tickflow.TickFlowProvider.fetch_daily_histories``
（与生产同一条代码路径，含 vendor 代码转换/限流/分批）。

用法（项目根目录）：
    .venv/bin/python research/2026-09-16_stock-universe-expansion/scan_liquidity.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _common  # noqa: E402
from data.provider_tickflow import TickFlowProvider  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "liquidity_20d.csv"
WINDOW_DAYS = 20


def load_symbols(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT symbol FROM stock_industry ORDER BY symbol").fetchall()
    conn.close()
    return [r[0] for r in rows]


def main() -> int:
    symbols = load_symbols(_common.DB_PATH)
    print(f"标的池：{len(symbols)} 只", flush=True)

    end = date.today()
    start = end - timedelta(days=60)  # 预留假期，取回后截尾部 20 根
    provider = TickFlowProvider()
    data, errors = provider.fetch_daily_histories(
        symbols, start, end, "none", batch_size=100, request_interval_seconds=0.0
    )
    provider.close()
    print(f"取回 {len(data)} 只，失败 {len(errors)} 只", flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows_out = []
    for sym in symbols:
        df = data.get(sym)
        if df is None or df.empty or "amount" not in df.columns:
            rows_out.append((sym, "", 0))
            continue
        tail = df.tail(WINDOW_DAYS)
        amounts = [a for a in tail["amount"].tolist() if isinstance(a, (int, float)) and a and a > 0]
        avg_yi = round(sum(amounts) / len(amounts) / 1e8, 4) if amounts else ""
        rows_out.append((sym, avg_yi, len(amounts)))

    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", f"avg_amount_{WINDOW_DAYS}d_yi", "bars_used"])
        writer.writerows(rows_out)

    got = sum(1 for r in rows_out if r[1] != "")
    print(f"写出 {len(rows_out)} 行（有均额 {got} 只）→ {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
