"""回填批量回测格子的新指标（2026-08）。

2026-08-24 起格子新增 5 列：benchmark_sharpe / benchmark_calmar /
excess_sharpe / excess_calmar / avg_flat_days。新批次由引擎直接落库，
本脚本为旧批次回填，无需重跑策略：

- benchmark_sharpe / benchmark_calmar：按格子实际起止日期（库里存的
  start_date/end_date 即窗口首末 bar）重建买入持有净值，复用引擎
  _buy_and_hold_benchmark + compute_summary，口径与新批次一致；
- excess_sharpe / excess_calmar = 策略值 − 基准值（逐格子）；
- avg_flat_days：由格子 trades_json + 区间交易日序列算空仓段均值，
  与 compute_summary 内同一函数（flat_run_days）计算。

只处理 status='ok' 且新列尚为 NULL 的格子，幂等可重跑。

用法（项目根目录，先确认没有批次在跑）：
    .venv/Scripts/python scripts/backfill_batch_excess_metrics.py --dry-run
    .venv/Scripts/python scripts/backfill_batch_excess_metrics.py
    .venv/Scripts/python scripts/backfill_batch_excess_metrics.py --batch-id 20260820...

回滚：恢复 data/backups/ 下脚本自动生成的备份文件即可。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from data.storage.db import init_db  # noqa: E402
from data.storage.market_store import MarketStore  # noqa: E402
from rule_backtest.engine import SingleSymbolAllInBacktestEngine  # noqa: E402
from rule_backtest.metrics import compute_summary, flat_run_days  # noqa: E402

DB_PATH = Path("data/trend_quant.db")
BACKUP_DIR = Path("data/backups")


def _slice_window(bars: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """按格子起止日期切出窗口 bars（日期一律取前 10 位比较）。

    补上引擎约定的 date 列（_buy_and_hold_benchmark 直接用 row["date"]）。
    """
    if bars.empty:
        return bars
    days = bars["time"].astype(str).str[:10]
    mask = pd.Series(True, index=bars.index)
    if start:
        mask &= days >= str(start)[:10]
    if end:
        mask &= days <= str(end)[:10]
    window = bars[mask].copy()
    if not window.empty and "date" not in window.columns:
        window["date"] = pd.to_datetime(window["time"], errors="coerce").dt.date
    return window


def _benchmark_sharpe_calmar(
    window_bars: pd.DataFrame, initial_capital: float, lot_size: int
) -> tuple[float | None, float | None]:
    bench = SingleSymbolAllInBacktestEngine._buy_and_hold_benchmark(
        bars=window_bars, initial_capital=initial_capital, lot_size=lot_size
    )
    nav = (bench or {}).get("series", [])
    if not nav:
        return None, None
    summary = compute_summary(daily_nav=nav, trades=[], turnover_total=0.0)
    return summary.get("sharpe"), summary.get("calmar")


def _avg_flat_days(trades_json: str | None, window_bars: pd.DataFrame) -> float | None:
    try:
        trades = json.loads(trades_json or "[]")
    except (ValueError, TypeError):
        trades = []
    if not trades:
        return None
    dates = [str(t)[:10] for t in window_bars["time"].tolist()]
    runs = flat_run_days(trades, dates)
    return sum(runs) / len(runs) if runs else 0.0


def backfill(batch_id: str | None, dry_run: bool) -> None:
    db = init_db()  # 默认路径 data/trend_quant.db，与 app 启动一致（初始化时自动补新列）
    store = MarketStore(db=db)

    where = "status = 'ok' AND (excess_sharpe IS NULL OR avg_flat_days IS NULL)"
    params: tuple = ()
    if batch_id:
        where += " AND batch_id = ?"
        params = (batch_id,)
    with db._connect() as conn:
        cells = conn.execute(
            f"""SELECT batch_id, symbol, strategy_id, start_date, end_date,
                       sharpe, calmar, trades_json
                FROM batch_backtest_cells WHERE {where}""",
            params,
        ).fetchall()
        batch_cfg = {
            r["batch_id"]: json.loads(r["config_json"] or "{}")
            for r in conn.execute(
                "SELECT batch_id, config_json FROM batch_backtest_runs"
            ).fetchall()
        }

    print(f"待回填格子：{len(cells)}")
    if dry_run:
        batches = sorted({c["batch_id"] for c in cells})
        for b in batches:
            n = sum(1 for c in cells if c["batch_id"] == b)
            print(f"  批次 {b}: {n} 格")
        return

    bars_cache: dict[str, pd.DataFrame] = {}

    def bars_for(symbol: str) -> pd.DataFrame:
        if symbol not in bars_cache:
            bars_cache[symbol] = store.load_history(symbol)
        return bars_cache[symbol]

    updated = 0
    skipped = 0
    with db._connect() as conn:
        for i, cell in enumerate(cells, 1):
            cfg = batch_cfg.get(cell["batch_id"], {})
            capital = float(cfg.get("initial_capital", 100_000.0))
            lot = int(cfg.get("lot_size", 100))
            window = _slice_window(bars_for(cell["symbol"]), cell["start_date"], cell["end_date"])
            if window.empty:
                skipped += 1
                continue
            bench_sharpe, bench_calmar = _benchmark_sharpe_calmar(window, capital, lot)
            flat_days = _avg_flat_days(cell["trades_json"], window)
            sharpe, calmar = cell["sharpe"], cell["calmar"]
            excess_sharpe = (
                float(sharpe) - float(bench_sharpe)
                if sharpe is not None and bench_sharpe is not None
                else None
            )
            excess_calmar = (
                float(calmar) - float(bench_calmar)
                if calmar is not None and bench_calmar is not None
                else None
            )
            conn.execute(
                """UPDATE batch_backtest_cells
                   SET benchmark_sharpe = ?, benchmark_calmar = ?,
                       excess_sharpe = ?, excess_calmar = ?, avg_flat_days = ?
                   WHERE batch_id = ? AND symbol = ? AND strategy_id = ?""",
                (
                    bench_sharpe, bench_calmar, excess_sharpe, excess_calmar, flat_days,
                    cell["batch_id"], cell["symbol"], cell["strategy_id"],
                ),
            )
            updated += 1
            if i % 500 == 0:
                print(f"  进度 {i}/{len(cells)}")
    print(f"完成：更新 {updated} 格，跳过（窗口无行情）{skipped} 格")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=None, help="只回填指定批次")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--no-backup", action="store_true", help="跳过自动备份")
    args = parser.parse_args()

    if not args.dry_run and not args.no_backup and DB_PATH.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = BACKUP_DIR / f"trend_quant_before_excess_metrics_{stamp}.db"
        shutil.copy2(DB_PATH, target)
        print(f"已备份数据库 → {target}")

    backfill(args.batch_id, args.dry_run)


if __name__ == "__main__":
    main()
