"""阶段 4：实盘运行器薄版（详设 §5.7）——清单产出 + 对账。

验证：同一策略对象/同一条决策代码路径（与回测器共享 evaluate_exits /
select_entries）；manual_trades 记账重建；清单落库幂等；对账价差与漏执行。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from portfolio.live import generate_daily_list, reconcile_daily_list
from portfolio.registry import fresh_registry
from portfolio.seed import seed_default_library
from portfolio.slots import register_builtin_modules

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db, symbol, closes, start="2023-01-02", asset_type="etf"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 2e6), "amount": closes * 2e6 * 5,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


@pytest.fixture
def market(test_db):
    rng = np.random.default_rng(9)
    for i in range(6):
        drift = 0.003 if i % 2 == 0 else 0.0005
        closes = 15 * np.exp(np.cumsum(rng.normal(drift, 0.015, 320)))
        _write_symbol(test_db, f"LIV{i:03d}.SS", closes)
    return test_db


def _seed(market, registry):
    return seed_default_library(market, registry)


def _user(market):
    return market.create_user("liver", "pass12345")


def test_generate_and_reconcile(market, registry):
    versions = _seed(market, registry)
    user = _user(market)
    as_of = datetime(2024, 3, 15, 14, 0)

    target = generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=as_of, initial_capital=1_000_000,
    )
    assert target["strategy_version_id"] == versions["base-v1"]
    assert "sells" in target and "buys" in target and "target_holdings" in target
    # 落库幂等（同日同策略 upsert）
    target2 = generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=as_of, initial_capital=1_000_000,
    )
    assert target2["buys"] == target["buys"]

    # 对账：没有任何 manual_trades → 全部应买未买
    rec = reconcile_daily_list(
        market, list_date="2024-03-15", strategy_version_id=versions["base-v1"],
        user_id=user["id"],
    )
    assert rec is not None
    assert len(rec["missing_buys"]) == len(target["buys"])
    assert rec["price_diffs"] == []

    # 用户按清单买入第一个标的（价格略高于收盘基准 → 执行差；GLM53F-P2-18：
    # 对账分母 = ref_price 收盘基准价，不再是含模型滑点的 est_price）
    if target["buys"]:
        first = target["buys"][0]
        market.create_manual_trade(
            user["id"], first["symbol"], "2024-03-15",
            round(first["ref_price"] * 1.004, 4), first["qty"],
        )
        rec2 = reconcile_daily_list(
            market, list_date="2024-03-15", strategy_version_id=versions["base-v1"],
            user_id=user["id"],
        )
        assert first["symbol"] not in rec2["missing_buys"]
        assert len(rec2["price_diffs"]) == 1
        assert rec2["price_diffs"][0]["bench_price"] == pytest.approx(first["ref_price"])
        assert rec2["price_diffs"][0]["diff_pct"] == pytest.approx(0.004, abs=1e-4)


def test_rebuild_account_uses_manual_trades(market, registry):
    """持仓重建：manual_trades 开仓记录 → 持仓 + 现金一致。"""
    versions = _seed(market, registry)
    user = _user(market)
    # 用户持有 LIV000 1000 股 @某价（昨日买入）
    market.create_manual_trade(user["id"], "LIV000.SS", "2024-03-14", 20.0, 1000)
    as_of = datetime(2024, 3, 15, 14, 0)
    target = generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=as_of, initial_capital=100_000,
    )
    # 持仓里应含 LIV000.SS（target_holdings 或 sells 中）
    involved = set(target["target_holdings"]) | {s["symbol"] for s in target["sells"]}
    assert "LIV000.SS" in involved


def test_live_list_requires_existing_strategy(market, registry):
    _seed(market, registry)
    user = _user(market)
    from portfolio.library import LibraryError

    with pytest.raises(LibraryError, match="not found"):
        generate_daily_list(
            market, strategy_version_id="ghost@9", user_id=user["id"],
            as_of=datetime(2024, 3, 15, 14, 0),
        )
