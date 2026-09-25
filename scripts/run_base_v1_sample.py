"""阶段 2 验收脚本：基准策略 v1 跑通 sample 窗口（2015-01-01 ~ 2024-12-31），
并与买入持有 / 60-40 / 随机入场（猴子基准）同图对比。

用法：
    .venv/Scripts/python.exe scripts/run_base_v1_sample.py [--short]

--short 只跑 2023-01-01 ~ 2024-12-31（冒烟用）。
产物：
- data/research/base_v1_sample/comparison.json（四条 NAV 曲线 + 指标表）
- data/research/base_v1_sample/comparison.md（指标对比表，验收快照）
- 控制台打印摘要。

说明（详设 §6.6.4 长窗口三注记）：
1. 覆盖率——2015–2019 段仅长历史子集参与（库内约 127 只有 2015 前数据，
   约占全池 1/7），该段结论必须携带覆盖率警告；
2. 费用时代错配——2015–2019 段真实费率高于当前默认，该段成本被低估，
   结论按保守方向解读；
3. 幸存者偏差加权——窗口越长，早期段"只含活到今天的标的"的偏差越重。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _arg_value(flag: str):
    """读 --db <value>；值缺失（末位无值）直接报错退出——静默回退默认库
    会把运行产物写进生产库（loop-review R2-P3-8）。"""
    for i, arg in enumerate(sys.argv):
        if arg == flag:
            if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("--"):
                raise SystemExit(f"{flag} requires a value (refusing to fall back to default db)")
            return sys.argv[i + 1]
    return None

from data.storage.db import Database
from engine.store import EngineStore
from portfolio import library
from portfolio.backtester import run_backtest
from portfolio.reports import benchmark_relative
from portfolio.seed import seed_default_library
from portfolio.slots import REGISTRY, ensure_builtins
from portfolio.strategy import parse_strategy_yaml
from rule_backtest.metrics import compute_summary

OUT_DIR = ROOT / "data" / "research" / "base_v1_sample"

# 怀疑阶梯上本次验收的三条 benchmark（§5.5）：买入持有 / 60-40 / 随机入场
BENCH_IDS = ["bench-buy-hold-csi300", "bench-60-40", "bench-random-entry"]


def _nav_from_db(db, run_id: str) -> list[dict]:
    return EngineStore.load_nav(db, run_id)


def _summarize(nav: list[dict]) -> dict:
    summary = compute_summary(nav, trades=[], turnover_total=0.0)
    # 退化腿闸门：本脚本把指标写进 data/research/base_v1_sample/comparison.json
    # 与 comparison.md（落盘物化产物），单调/极短腿的年化 sharpe 是浮点残差噪声
    # → 超幅值闸门即记 None。calmar 不入闸（低回撤/短窗口可合法 > 50）。
    from rule_backtest.metrics import DEGENERATE_SHARPE_ABS_LIMIT

    def _gated(value):
        if value is None:
            return None
        return None if abs(float(value)) > DEGENERATE_SHARPE_ABS_LIMIT else round(value, 3)

    return {
        "annual_return": round(summary["annual_return"], 4),
        "total_return": round(summary["total_return"], 4),
        "max_drawdown": round(summary["max_drawdown"], 4),
        "sharpe": _gated(summary["sharpe"]),
        "sortino": _gated(summary["sortino"]),
        "calmar": round(summary["calmar"], 3),
    }


def main() -> int:
    short = "--short" in sys.argv
    db_path = _arg_value("--db")  # 评审 DS-P2-10：可指向独立库（默认仍是系统库）
    start = date(2023, 1, 1) if short else date(2015, 1, 1)
    end = date(2024, 12, 31)

    db = Database(db_path) if db_path else Database()
    ensure_builtins()
    versions = seed_default_library(db, REGISTRY)
    print(f"[seed] {len(versions)} 条策略线入库: {sorted(versions)[:3]} ...")

    targets = ["base-v1"] + BENCH_IDS
    results: dict[str, dict] = {}
    for sid in targets:
        row = library.get_version(db, versions[sid])
        cfg = parse_strategy_yaml(row["config_yaml"], REGISTRY)
        t0 = time.monotonic()
        result = run_backtest(
            db, config=cfg, registry=REGISTRY,
            start=start, end=end, initial_capital=1_000_000,
            strategy_ref=versions[sid],
        )
        elapsed = time.monotonic() - t0
        nav = result["daily_nav"]
        results[sid] = {
            "run_id": result["run_id"],
            "summary": _summarize(nav),
            "trades": len(result["trades"]),
            "unfilled": len(result["unfilled"]),
            "elapsed_s": round(elapsed, 1),
            "nav": [{"date": r["date"], "equity": round(r["equity"], 2)} for r in nav],
        }
        print(
            f"[run] {sid:26s} final={nav[-1]['equity']:>12,.0f} "
            f"annual={results[sid]['summary']['annual_return']:>7.2%} "
            f"mdd={results[sid]['summary']['max_drawdown']:>7.2%} "
            f"sharpe={results[sid]['summary']['sharpe']:>5.2f} "
            f"trades={len(result['trades']):>4d} unfilled={len(result['unfilled']):>3d} "
            f"({elapsed:.1f}s)"
        )

    # 相对基准统计（v1 vs 买入持有）
    base_nav = _nav_from_db(db, results["base-v1"]["run_id"])
    relatives = {}
    for sid in BENCH_IDS:
        relatives[sid] = benchmark_relative(
            base_nav, _nav_from_db(db, results[sid]["run_id"])
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    comparison = {
        "window": [start.isoformat(), end.isoformat()],
        "initial_capital": 1_000_000,
        "caveats": [
            "覆盖率：2015–2019 段仅长历史子集参与（约 1/7 池），结论带覆盖率警告",
            "费用时代错配：2015–2019 段真实费率更高，该段成本被低估",
            "幸存者偏差加权：早期段只含活到今天的标的，结论按段打折",
        ],
        "results": results,
        "base_v1_vs_benchmarks": relatives,
    }
    (OUT_DIR / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    lines = [
        f"# base-v1 vs benchmarks（{start} ~ {end}，初始资金 100 万）",
        "",
        "| 策略 | 年化 | 总收益 | 最大回撤 | Sharpe | Sortino | 成交笔数 | 未成交 | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sid in targets:
        s = results[sid]["summary"]
        lines.append(
            f"| {sid} | {s['annual_return']:.2%} | {s['total_return']:.2%} | "
            f"{s['max_drawdown']:.2%} | {s['sharpe']:.2f} | {s['sortino']:.2f} | "
            f"{results[sid]['trades']} | {results[sid]['unfilled']} | {results[sid]['elapsed_s']} |"
        )
    lines += [
        "",
        "三注记（详设 §6.6.4）：① 2015–2019 段覆盖率约 1/7 池，结论带覆盖率警告；"
        "② 早期段真实费率更高、成本被低估；③ 幸存者偏差随窗口长度加权。",
    ]
    (OUT_DIR / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[out] {OUT_DIR / 'comparison.json'} / comparison.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
