"""旧批次 round-trip 回填（方案 2026-08-30 §2.3 任务 8）。

2026-08-30 起新批次由引擎直接落 round_trips_json；本脚本为旧批次从
trades_json + qfq 行情重放推导，无需重跑策略：

- 只处理 status='ok' 且 round_trips_json IS NULL 的格子，幂等可重跑；
- 回填行 round_trips_source='backfill'（区分引擎直采 'engine'）；
- round-trip 维度平铺指标（r_mean 等）一并回填；日度 NAV 维度
  （cvar_5/ulcer_index/max_dd_duration_days）无法回填，保持 NULL；
- 入场 ATR 为旧口径（含入场日当根），与新批次 prev_close 口径不同 ——
  导出 manifest 的 atr_basis 字段已区分。

追求口径纯净就全部重跑，别回填（方案 §10 开放问题 1，拍板：回填+标注）。

用法（项目根目录，先确认没有批次在跑）：
    .venv/Scripts/python scripts/backfill_round_trips.py --dry-run
    .venv/Scripts/python scripts/backfill_round_trips.py
    .venv/Scripts/python scripts/backfill_round_trips.py --batch-id 20260820...

回滚：恢复 data/backups/ 下脚本自动生成的备份文件即可。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _common  # .env 加载 + DB_PATH（P2-13）
import pandas as pd

from data.storage.db import Database, init_db
from data.storage.market_store import MarketStore
from rule_backtest.metrics import compute_roundtrip_stats
from rule_backtest.roundtrip_replay import replay_round_trips

DB_PATH = _common.DB_PATH
BACKUP_DIR = Path("data/backups")

_RT_STAT_COLUMNS = (
    "r_mean", "r_p5", "r_p25", "r_p75", "r_p95", "r_skew", "tail_ratio",
    "exit_efficiency", "max_losing_streak", "stop_exit_ratio", "chandelier_exit_ratio",
)


def backfill(batch_id: str | None, dry_run: bool) -> None:
    db = init_db()  # 初始化时自动补新列（幂等迁移）
    store = MarketStore(db=db)

    where = "status = 'ok' AND round_trips_json IS NULL"
    params: tuple = ()
    if batch_id:
        where += " AND batch_id = ?"
        params = (batch_id,)
    with db._connect() as conn:
        cells = conn.execute(
            f"""SELECT batch_id, symbol, strategy_id, asset_type, category_l1, trades_json
                FROM batch_backtest_cells WHERE {where}""",
            params,
        ).fetchall()
        snapshots = {
            r["batch_id"]: {
                s["id"]: s.get("strategy_config", {})
                for s in json.loads(r["strategy_snapshot_json"] or "[]")
            }
            for r in conn.execute(
                "SELECT batch_id, strategy_snapshot_json FROM batch_backtest_runs"
            ).fetchall()
        }

    print(f"待回填格子：{len(cells)}")
    if dry_run:
        for b in sorted({c["batch_id"] for c in cells}):
            n = sum(1 for c in cells if c["batch_id"] == b)
            print(f"  批次 {b}: {n} 格")
        return

    bars_cache: dict[str, pd.DataFrame] = {}

    def bars_for(symbol: str) -> pd.DataFrame:
        if symbol not in bars_cache:
            bars_cache[symbol] = store.load_history(symbol)
        return bars_cache[symbol]

    updated = 0
    with db._connect() as conn:
        for i, cell in enumerate(cells, 1):
            strategy_cfg = snapshots.get(cell["batch_id"], {}).get(cell["strategy_id"], {})
            try:
                trades = json.loads(cell["trades_json"] or "[]")
            except (ValueError, TypeError):
                trades = []
            trips = replay_round_trips(
                trades,
                bars_for(cell["symbol"]),
                strategy_cfg,
                asset_type=str(cell["asset_type"] or "etf"),
                category_l1=str(cell["category_l1"] or ""),
            )
            stats = compute_roundtrip_stats(trips, [])
            conn.execute(
                f"""UPDATE batch_backtest_cells
                    SET round_trips_json = ?, round_trips_source = 'backfill',
                        {", ".join(f"{c} = ?" for c in _RT_STAT_COLUMNS)}
                    WHERE batch_id = ? AND symbol = ? AND strategy_id = ?""",
                (
                    json.dumps(trips, ensure_ascii=False),
                    *(stats[c] for c in _RT_STAT_COLUMNS),
                    cell["batch_id"], cell["symbol"], cell["strategy_id"],
                ),
            )
            updated += 1
            if i % 500 == 0:
                print(f"  进度 {i}/{len(cells)}")
    print(f"完成：回填 {updated} 格（round_trips_source='backfill'）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=None, help="只回填指定批次")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--no-backup", action="store_true", help="跳过自动备份")
    args = parser.parse_args()

    if not args.dry_run and not args.no_backup and DB_PATH.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # VACUUM INTO 在线备份（WAL 安全；P2-24）
        target = Database(DB_PATH).backup_to(backup_dir=BACKUP_DIR, keep=10)
        print(f"已备份数据库 → {target}")

    backfill(args.batch_id, args.dry_run)


if __name__ == "__main__":
    main()
