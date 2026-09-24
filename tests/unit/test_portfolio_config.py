"""L3 策略配置 / 注册表 / 策略库单元测试（详设 §5.2/§5.3/§5.5）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio import library
from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules
from portfolio.strategy import (
    StrategyConfigError,
    apply_diff,
    parse_strategy_yaml,
)

pytestmark = pytest.mark.unit

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "src" / "portfolio" / "strategies"


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


BASE_CFG = """
name: t1
universe: {module: category_filter@1, params: {asset_type: etf}}
signal: {module: macd_cross@1, params: {fast: 12, slow: 26, signal: 9, use_exit: false}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: equal_risk@1, params: {risk_budget_pct: 0.0075}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 10}}
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {slippage_base: 0.002, slippage_tail: 0.001}}
"""


def test_base_v1_yaml_parses_with_builtins(registry):
    cfg = parse_strategy_yaml((STRATEGIES_DIR / "base_v1.yaml").read_text(encoding="utf-8"), registry)
    assert cfg.slots["signal"].module == "macd_cross@1"
    assert cfg.slots["signal"].params["use_exit"] is False
    assert cfg.slots["position_risk"].module == "hard_stop@1"
    assert cfg.slots["position_risk"].params["atr_mul"] == 1.5
    assert cfg.gates == []


def test_all_benchmark_yamls_parse(registry):
    for f in STRATEGIES_DIR.glob("bench_*.yaml"):
        cfg = parse_strategy_yaml(f.read_text(encoding="utf-8"), registry)
        assert cfg.slots["signal"].module is not None, f"{f.name} missing signal"


def test_illegal_config_rejected_at_load(registry):
    # 未注册模块
    with pytest.raises(StrategyConfigError):
        parse_strategy_yaml(BASE_CFG.replace("macd_cross@1", "ghost@9"), registry)
    # 参数越界（risk_budget_pct > max）
    with pytest.raises(StrategyConfigError):
        parse_strategy_yaml(
            BASE_CFG.replace("risk_budget_pct: 0.0075", "risk_budget_pct: 0.9"), registry
        )
    # 未知顶层键
    with pytest.raises(StrategyConfigError):
        parse_strategy_yaml(BASE_CFG + "bogus_key: 1\n", registry)
    # 模块放错槽
    with pytest.raises(StrategyConfigError):
        parse_strategy_yaml(
            BASE_CFG.replace("module: hard_stop@1", "module: macd_cross@1"), registry
        )


def test_config_hash_stable_and_sensitive(registry):
    a = parse_strategy_yaml(BASE_CFG, registry)
    b = parse_strategy_yaml(BASE_CFG, registry)
    assert a.config_hash() == b.config_hash()
    c = parse_strategy_yaml(BASE_CFG.replace("atr_mul: 1.5", "atr_mul: 2.0"), registry)
    assert c.config_hash() != a.config_hash()


def test_apply_diff_single_slot(registry):
    base = parse_strategy_yaml(BASE_CFG, registry)
    new = apply_diff(base, [{"slot": "signal", "to": "ma_cross@1", "params": {"n": 20}}], registry)
    assert new.slots["signal"].module == "ma_cross@1"
    assert new.slots["signal"].params["n"] == 20
    assert new.slots["position_risk"].module == "hard_stop@1"  # 其余槽不动
    assert new.config_hash() != base.config_hash()


def test_apply_diff_portfolio_risk_append_and_replace(registry):
    base = parse_strategy_yaml(BASE_CFG, registry)
    added = apply_diff(base, [{"slot": "portfolio_risk", "to": "heat_cap@1",
                               "params": {"max_heat_pct": 0.06}}], registry)
    assert len(added.gates) == 2
    replaced = apply_diff(base, [{"slot": "portfolio_risk",
                                  "to": [{"module": "heat_cap@1", "params": {"max_heat_pct": 0.05}}]}],
                          registry)
    assert len(replaced.gates) == 1
    assert replaced.gates[0].module == "heat_cap@1"


def test_meta_module_any_of_position_risk(registry):
    cfg = parse_strategy_yaml(
        BASE_CFG.replace(
            "{module: hard_stop@1, params: {atr_mul: 1.5}}",
            "{module: any_of@1, params: {members: [{module: hard_stop@1, params: {atr_mul: 1.5}},"
            " {module: time_stop@1, params: {max_days: 30}}]}}",
        ),
        registry,
    )
    assert cfg.slots["position_risk"].module == "any_of@1"
    # 元模块嵌套元模块：禁
    with pytest.raises(StrategyConfigError):
        parse_strategy_yaml(
            BASE_CFG.replace(
                "{module: hard_stop@1, params: {atr_mul: 1.5}}",
                "{module: any_of@1, params: {members: [{module: any_of@1, params: {members: []}}]}}",
            ),
            registry,
        )


def test_seed_library_idempotent(test_db, registry):
    first = seed_default_library(test_db, registry)
    second = seed_default_library(test_db, registry)
    assert first == second
    assert len(first) == 9  # blank-base + base-v1 + 7 benchmarks
    v = library.get_version(test_db, first["base-v1"])
    assert v is not None and v["version"] == 1
    # 版本不可变（触发器）
    with test_db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE portfolio_strategy_versions SET config_yaml='x' WHERE id=?",
            (first["base-v1"],),
        )


def test_registry_slot_scoped_meta_modules(registry):
    """any_of 在 signal 与 position_risk 各有一份实现（slot 消歧）。"""
    sig = registry.require("any_of@1", slot="signal")
    pos = registry.require("any_of@1", slot="position_risk")
    assert sig.factory is not pos.factory
    assert registry.has("any_of@1")
