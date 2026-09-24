"""七插槽内置模块注册入口（详设 §5.14 首批预置模块清单）。

导入本包即把全部内置模块注册进 REGISTRY（进程级唯一注册表）。
"""

from __future__ import annotations

from portfolio.registry import REGISTRY, ModuleRegistry
from portfolio.slots import (
    execution,
    portfolio_risk,
    position_risk,
    rank,
    signal,
    sizing,
    universe,
)

_registered = False


def register_builtin_modules(registry: ModuleRegistry = REGISTRY) -> None:
    universe.register_universe_modules(registry)
    signal.register_signal_modules(registry)
    rank.register_rank_modules(registry)
    sizing.register_sizing_modules(registry)
    portfolio_risk.register_portfolio_risk_modules(registry)
    position_risk.register_position_risk_modules(registry)
    execution.register_execution_modules(registry)
    execution.register_meta_modules(registry)


def ensure_builtins() -> None:
    global _registered
    if not _registered:
        register_builtin_modules(REGISTRY)
        _registered = True


ensure_builtins()
