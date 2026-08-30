"""止损倍数 sweep 跑批（方案 2026-08-30 §5.1/§6.4，极低频操作，不做页面入口）。

对每个策略 × sweep_atr_muls 每值生成一份快照（chandelier 固定 = hard × 2），
批次格子数 ×N，用于参数敏感性分析（导出后画 7 点折线，判断尖峰 vs 平台）。

用法（项目根目录；耗时与格子数成正比，**运行中可安全关闭** —— 已完成的格子
已逐格落库，中断的半成品批次在页面删除即可）：
    .venv/Scripts/python scripts/run_stop_sweep.py --categories 行业ETF --strategy-ids macd_xxx
    .venv/Scripts/python scripts/run_stop_sweep.py ... --muls 0.75,1.0,1.5,2.0 --cost-multiplier 2
    # 样本内/外（§6.4.3）：跑两遍，比对选档规则是否稳定
    .venv/Scripts/python scripts/run_stop_sweep.py ... --end-date 2023-12-31
    .venv/Scripts/python scripts/run_stop_sweep.py ... --start-date 2024-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _common  # .env 加载 + DB_PATH（P2-13）  # noqa: F401

from data.storage.db import init_db
from rule_backtest.batch_service import BatchBacktestService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", required=True, help="一级类目，逗号分隔")
    parser.add_argument("--strategy-ids", required=True, help="策略 ID，逗号分隔")
    parser.add_argument("--muls", default="", help="sweep 的 atr_mul 列表（默认 0.75~3.0 七档）")
    parser.add_argument("--name", default="", help="批次名（默认自动生成 + [sweep] 后缀）")
    parser.add_argument("--start-date", default="", help="回测开始日期 YYYY-MM-DD（样本外起点）")
    parser.add_argument("--end-date", default="", help="回测结束日期 YYYY-MM-DD（样本内终点）")
    parser.add_argument("--cost-multiplier", type=float, default=1.0,
                        help="成本压力测试：费率/滑点/印花税 ×N（§6.4.2）")
    args = parser.parse_args()

    muls = [float(x) for x in args.muls.split(",") if x.strip()] or None
    start = date.fromisoformat(args.start_date) if args.start_date else None
    end = date.fromisoformat(args.end_date) if args.end_date else None

    db = init_db()
    service = BatchBacktestService(db=db)
    batch = service.prepare_batch(
        categories=[c.strip() for c in args.categories.split(",") if c.strip()],
        strategy_ids=[s.strip() for s in args.strategy_ids.split(",") if s.strip()],
        name=args.name,
        start_date=start,
        end_date=end,
        stop_profile="sweep",
        sweep_atr_muls=muls,
    )
    if args.cost_multiplier != 1.0:
        config = json.loads(batch["config_json"])
        for key in ("fee_rate", "fee_min", "slippage", "stock_stamp_tax_rate"):
            config[key] = float(config[key]) * args.cost_multiplier
        config["cost_multiplier"] = args.cost_multiplier
        batch["config_json"] = json.dumps(config)

    if not db.create_batch_run_if_idle(batch):
        print("已有批次正在运行，请等待完成或先在页面取消")
        raise SystemExit(1)

    batch_id = batch["batch_id"]
    print(f"sweep 批次 {batch_id}（{batch['name']}）：{batch['total_cells']} 格")
    print("运行中可安全关闭（Ctrl+C）：已完成格子已落库，半成品批次在页面删除即可")
    try:
        t0 = time.time()
        service.run_batch(batch_id)
        run = db.get_batch_run(batch_id)
        print(
            f"完成：{run['status']}，ok={run['ok_cells']} failed={run['failed_cells']} "
            f"skipped={run['skipped_cells']}，耗时 {time.time() - t0:.0f}s"
        )
        print(f"导出：.venv/Scripts/python scripts/export_backtest_analysis.py {batch_id}")
    except KeyboardInterrupt:
        print("\n已中断；已完成格子已落库，半成品批次可在页面删除")


if __name__ == "__main__":
    main()
