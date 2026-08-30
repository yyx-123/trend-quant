"""导出批次回测分析数据（方案 2026-08-30 §8）：long-format CSV + manifest，供 AI 离线分析。

用法（项目根目录）：
    .venv/Scripts/python scripts/export_backtest_analysis.py <batch_id>
    .venv/Scripts/python scripts/export_backtest_analysis.py <batch_id> --compare <alt_batch_id>
    .venv/Scripts/python scripts/export_backtest_analysis.py <batch_id> --out exports/ --no-live

输出目录 exports/batch_<id>[_vs_<id>]/：manifest.json / cells.csv / round_trips.csv /
annual_aggregates.csv / stop_diagnostics.csv / live_trades.csv（可选）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _common  # .env 加载 + DB_PATH（P2-13）  # noqa: F401

from data.storage.db import init_db
from services.backtest_export import export_batch_analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_id", help="要导出的批次 ID")
    parser.add_argument("--compare", default=None, help="对比批次 ID（紧/松并排）")
    parser.add_argument("--out", default="exports", help="输出根目录（默认 exports/）")
    parser.add_argument("--no-live", action="store_true", help="不导出实盘逐笔 live_trades.csv")
    args = parser.parse_args()

    db = init_db()
    try:
        result = export_batch_analysis(
            db,
            args.batch_id,
            alt_batch_id=args.compare,
            out_dir=Path(args.out),
            include_live=not args.no_live,
        )
    except ValueError as exc:
        print(f"导出失败：{exc}")
        raise SystemExit(1)

    print(f"导出完成 → {result['export_dir']}")
    for name, rows in result["files"].items():
        print(f"  {name}: {rows} 行" if name.endswith(".csv") else f"  {name}")


if __name__ == "__main__":
    main()
