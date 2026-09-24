"""策略库种子（详设 §5.5）：blank-base + base-v1 + 7 条 benchmark 入库。

benchmark 按"怀疑阶梯"组织，每个回答一个具体问题；benchmark 与正式策略
同构（同样是七插槽配置）——"无脑默认"在插槽级与策略级都是一等公民。
幂等：config_hash 唯一约束下重复种子不产生新版本。
"""

from __future__ import annotations

from pathlib import Path

from portfolio import library
from portfolio.registry import ModuleRegistry

STRATEGIES_DIR = Path(__file__).resolve().parent / "strategies"

# (strategy_id, yaml 文件, is_benchmark, is_blank_base, 描述)
SEEDS: tuple[tuple[str, str, bool, bool, str], ...] = (
    ("blank-base", "blank_base.yaml", False, True, "空白基准：七插槽全 none（创建型实验的 diff 起点）"),
    ("base-v1", "base_v1.yaml", False, False, "基准策略 v1：当前实盘打法 1:1 复刻（决策 5）"),
    ("bench-buy-hold-csi300", "bench_buy_hold_csi300.yaml", True, False, "买入并持有沪深300ETF"),
    ("bench-60-40", "bench_bond_stock_60_40.yaml", True, False, "60/40 股债年度再平衡"),
    ("bench-sma200-timing", "bench_sma200_timing.yaml", True, False, "Faber SMA200 择时"),
    ("bench-momentum-rotation", "bench_simple_momentum_rotation.yaml", True, False, "简单动量轮动（月度）"),
    ("bench-random-entry", "bench_random_entry.yaml", True, False, "随机入场 + v1 同止损（猴子基准）"),
    ("bench-random-pick", "bench_random_pick.yaml", True, False, "MACD 候选 + 随机排序（rank 槽价值）"),
    ("bench-equal-weight", "bench_equal_weight_universe.yaml", True, False, "流动性 top-50 等权月度再平衡"),
)


def seed_default_library(db, registry: ModuleRegistry, *, created_by: str = "system") -> dict:
    """把种子配置全部入库；返回 {strategy_id: version_id}。"""
    from portfolio.slots import ensure_builtins

    ensure_builtins()
    out: dict[str, str] = {}
    for strategy_id, filename, is_benchmark, is_blank_base, desc in SEEDS:
        library.ensure_strategy(
            db, strategy_id, name=strategy_id, description=desc,
            is_benchmark=is_benchmark, is_blank_base=is_blank_base,
            created_by=created_by,
        )
        yaml_text = (STRATEGIES_DIR / filename).read_text(encoding="utf-8")
        version = library.add_version_yaml(
            db, strategy_id, yaml_text, registry, created_by=created_by
        )
        out[strategy_id] = version["id"]
    return out


def list_library(db) -> dict:
    """策略库总览（台账/看板查询面）。"""
    strategies = library.list_strategies(db, include_retired=True)
    return {
        "strategies": [
            {
                **s,
                "versions": [
                    {k: v[k] for k in ("id", "version", "config_hash", "parent_version_id", "experiment_id", "created_at")}
                    for v in library.list_versions(db, s["id"])
                ],
            }
            for s in strategies
        ]
    }
