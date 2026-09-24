"""L3 对外服务面（详设 §5.6）：L4 调用 L3 的唯一入口。

原则：L4 只提交声明式 spec，从不自己实现或组装策略对象——策略的组装、
校验、执行全部在 L3 内完成（分层隔离的实体化）。

- resolve_experiment_config(base_version_id, diff) → resolved_config
  （模块存在性、参数域、插槽完整性全部在此校验；config_hash 随结果返回）；
- run_backtest(resolved_config, run_params) → run_id（可同步执行；
  并发调度由 L4 worker 承担）；
- 可复现性规则：解析后的完整配置 YAML 随 run 落库（不是只存 hash）。
"""

from __future__ import annotations

from datetime import date

from portfolio import library
from portfolio.backtester import run_backtest as _run_backtest
from portfolio.registry import ModuleRegistry
from portfolio.strategy import StrategyConfig, apply_diff, parse_strategy_yaml



def load_fills(db, run_id: str) -> list[dict]:
    """L4 读成交的合法路径（L3 转发 L2 存储；分层铁律：L4 不跨级 import L2）。"""
    from engine.store import EngineStore

    return EngineStore.load_fills(db, run_id)


def load_nav(db, run_id: str) -> list[dict]:
    """L4 读净值的合法路径。"""
    from engine.store import EngineStore

    return EngineStore.load_nav(db, run_id)


def load_positions(db, run_id: str, day: str | None = None) -> list[dict]:
    """L4 读持仓快照的合法路径。"""
    from engine.store import EngineStore

    return EngineStore.load_positions(db, run_id, day=day)


def get_engine_run(db, run_id: str) -> dict | None:
    """L4 读运行档案的合法路径。"""
    from engine.store import EngineStore

    return EngineStore.get_run(db, run_id)


class ServiceError(ValueError):
    pass


def resolve_experiment_config(
    db,
    *,
    base_version_id: str,
    diff: list[dict],
    registry: ModuleRegistry,
    new_name: str | None = None,
) -> tuple[StrategyConfig, str]:
    """"基准 + 插槽 diff" → 完整策略配置（非法即拒）。

    返回 (config, resolved_yaml)。实验期策略是"解析即弃"的临时配置，
    但完整内容随 run 落库可复现（engine_runs.resolved_config_yaml）。
    """
    base_row = library.get_version(db, base_version_id)
    if base_row is None:
        raise ServiceError(f"base version not found: {base_version_id}")
    base_config = parse_strategy_yaml(base_row["config_yaml"], registry)
    config = apply_diff(base_config, diff, registry)
    if new_name:
        config = StrategyConfig(
            name=new_name, description=config.description,
            slots=config.slots, gates=config.gates,
        )
    return config, config.canonical_yaml()


def run_backtest(
    db,
    *,
    config: StrategyConfig,
    registry: ModuleRegistry,
    run_params: dict | None = None,
    strategy_ref: str = "",
) -> dict:
    """同步执行一次组合回测（run_params = 运行级参数：initial_capital /
    market_profile / window_kind / window；全部记入 engine_runs 血缘）。"""
    params = dict(run_params or {})
    window = params.get("window")
    if not window:
        raise ServiceError("run_params.window [start, end] is required")
    return _run_backtest(
        db,
        config=config,
        registry=registry,
        start=date.fromisoformat(str(window[0])[:10]),
        end=date.fromisoformat(str(window[1])[:10]),
        initial_capital=float(params.get("initial_capital", 1_000_000.0)),
        market_profile=str(params.get("market_profile", "cn_stock")),
        strategy_ref=strategy_ref,
        run_params=params,
        window_kind=(
            # R3B-P3-9：L3 侧同样卡枚举——直调 L3 不再把任意字符串写进
            # engine_runs 血缘（枚举真源与 research_runs CHECK 一致）
            lambda wk: wk if wk in ("sample", "holdout", "plateau_probe")
            else (_ for _ in ()).throw(ValueError(f"invalid window_kind: {wk!r}"))
        )(str(params.get("window_kind", "sample"))),
    )
