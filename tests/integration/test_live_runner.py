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


def test_live_buy_list_lot_aligned(market, registry):
    """loop-review R1-P1-5：清单买入数量必须整手（base-v1 的 equal_risk 产
    quantity 意图任意浮点股数；回测引擎 matcher 内对齐，清单路径此前只
    int() 截断，产出 16259 这类不可执行数量）。"""
    versions = _seed(market, registry)
    user = _user(market)
    as_of = datetime(2024, 3, 15, 14, 0)
    target = generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=as_of, initial_capital=1_000_000,
    )
    assert target["buys"], "fixture 应产出买入清单"
    for item in target["buys"]:
        assert item["qty"] % 100 == 0, f"{item['symbol']} qty={item['qty']} 非整手"


def test_live_account_rebuild_aggregates_same_symbol(market, registry):
    """loop-review R1-P2-1：同标的多次 open 的 manual_trades 聚合重建——
    不再静默覆盖（现金扣两笔、持仓只剩最后一笔的失真）。"""
    from portfolio.live import rebuild_account_from_manual_trades

    versions = _seed(market, registry)
    user = _user(market)
    market.create_manual_trade(user["id"], "LIV000.SS", "2024-03-13", 20.0, 1000)
    market.create_manual_trade(user["id"], "LIV000.SS", "2024-03-14", 22.0, 500)
    # 先产一次清单（确认持仓并入取数集的真实形态），再做重建对拍
    generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=datetime(2024, 3, 15, 14, 0), initial_capital=100_000,
    )
    from gateway.service import Gateway

    gateway = Gateway(market)
    as_of = datetime(2024, 3, 15, 14, 0)
    panel = gateway.get_panel(
        symbols=["LIV000.SS"], start=None, end=as_of.date(),
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="qfq", as_of=as_of, mode="live",
        caller_layer="live",
    )
    from portfolio.slots import ensure_builtins

    ensure_builtins()
    from portfolio.slots import REGISTRY as _R

    prisk = _R.require("hard_stop@1", slot="position_risk").factory({"atr_mul": 1.5})
    account = rebuild_account_from_manual_trades(
        market, user_id=user["id"], initial_capital=100_000,
        panel=panel, position_risk_module=prisk,
    )
    pos = account.positions.get("LIV000.SS")
    assert pos is not None and pos.quantity == 1500
    # 加权成本 = (1000×20 + 500×22) / 1500 ≈ 20.667
    assert pos.avg_cost == pytest.approx((1000 * 20.0 + 500 * 22.0) / 1500, abs=1e-6)
    assert pos.entry_date == pd.Timestamp("2024-03-13").date()  # 最早入场


def test_live_sellable_quantity_is_per_lot(market, registry):
    """R12B-F3：同标的多次买入时 T+1 可卖量必须**逐笔**判定。

    旧实现按聚合后的"最早入场日"判整笔：`昨日买 1000 + 今日买 1000` 得到
    sellable=2000（实际只有 1000 可卖），清单会下发 executable=true 的 2000 股
    卖出（人工照做被券商拒），shadow 现金也按 2000 股入账 → 买入清单被虚增现金放大。
    """
    from datetime import datetime as _dt

    from gateway.service import Gateway
    from portfolio.live import rebuild_account_from_manual_trades

    versions = _seed(market, registry)
    user = _user(market)
    # 昨日买 1000（可卖）+ 当日买 1000（T+1 不可卖）
    market.create_manual_trade(user["id"], "LIV000.SS", "2024-03-14", 20.0, 1000)
    market.create_manual_trade(user["id"], "LIV000.SS", "2024-03-15", 22.0, 1000)
    as_of = _dt(2024, 3, 15, 14, 0)
    generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=as_of, initial_capital=100_000,
    )
    panel = Gateway(market).get_panel(
        symbols=["LIV000.SS"], start=None, end=as_of.date(),
        fields=["open", "high", "low", "close", "volume", "amount"],
        adjust="qfq", as_of=as_of, mode="live", caller_layer="live",
    )
    from portfolio.slots import REGISTRY as _R

    prisk = _R.require("hard_stop@1", slot="position_risk").factory({"atr_mul": 1.5})
    account = rebuild_account_from_manual_trades(
        market, user_id=user["id"], initial_capital=100_000,
        panel=panel, position_risk_module=prisk,
    )
    pos = account.positions["LIV000.SS"]
    assert pos.quantity == 2000
    assert pos.sellable_quantity == 1000, "当日买入的那 1000 股不可卖（T+1）"


def test_live_panel_window_covers_old_positions(market, registry):
    """R12B-F2：持仓入场日早于 400 天窗口时必须仍能重建止损状态。

    旧实现固定 `pad_start = 决策日 − 400 天`：更早买入的持仓 `date_pos(buy_date)`
    返回 None → stop=None → 该持仓在清单里**永不产生止损卖出意图**（止损被静默取消）。
    修复后窗口起点前移到最早未平仓买入日 − 60 天。
    """
    from datetime import datetime as _dt

    versions = _seed(market, registry)
    user = _user(market)
    # 行情从 2023-01-02 起（320 根）；买在首日 → 距 2024-03-15 约 440 天 > 400
    first_day = market.load_market_data("LIV000.SS", price_mode="qfq").iloc[0]["time"]
    buy_date = str(pd.Timestamp(first_day).date())
    market.create_manual_trade(user["id"], "LIV000.SS", buy_date, 15.0, 1000)

    target = generate_daily_list(
        market, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=_dt(2024, 3, 15, 14, 0), initial_capital=100_000,
    )
    caveats = " ".join(target.get("caveats") or [])
    assert "持仓无止损价" not in caveats, f"老持仓的止损状态应已重建，实际 caveats: {caveats}"


def test_shadow_cash_excludes_unsellable_same_day_lot(market, registry, monkeypatch):
    """R13A-F1：影子现金不得按**不可卖**的当日买入股数入账。

    旧实现按 `pos.quantity` 全额入账 → "昨买 1000 + 今买 1000" 与"昨买 2000"
    得到同一个 cash_est（多算 1000 股市值）→ 买入清单被不存在的现金放大。
    这里把 `evaluate_exits` 打桩成"必触发止损"，直接比较两种入账形态。
    """
    from datetime import datetime as _dt

    from engine.models import ExitOrderIntent
    from portfolio.live import generate_daily_list

    versions = _seed(market, registry)

    def _list(user_id: int) -> dict:
        return generate_daily_list(
            market, strategy_version_id=versions["base-v1"], user_id=user_id,
            as_of=_dt(2024, 3, 15, 14, 0), initial_capital=1_000_000,
        )

    def _patch_exits(monkeypatch):
        import portfolio.live as live_mod

        def fake_evaluate_exits(ctx, modules, panel, t_idx):
            intents = [
                ExitOrderIntent(symbol=s, reason="hard_stop",
                                decision_date=panel.dates[t_idx])
                for s in list(ctx.account.positions)
            ]
            return intents, [], [], []

        monkeypatch.setattr(live_mod, "evaluate_exits", fake_evaluate_exits)

    _patch_exits(monkeypatch)
    # 两个用户：一个"昨买+今买"（当日那笔不可卖），一个"昨买+昨买"（都可卖）
    today = market.create_user("r13a_today", "pass12345")
    market.create_manual_trade(today["id"], "LIV000.SS", "2024-03-14", 20.0, 1000)
    market.create_manual_trade(today["id"], "LIV000.SS", "2024-03-15", 22.0, 1000)
    yesterday = market.create_user("r13a_yday", "pass12345")
    market.create_manual_trade(yesterday["id"], "LIV000.SS", "2024-03-14", 20.0, 1000)
    market.create_manual_trade(yesterday["id"], "LIV000.SS", "2024-03-14", 22.0, 1000)

    partial = _list(today["id"])        # 只有 1000 股可卖
    full = _list(yesterday["id"])       # 2000 股都可卖
    assert partial["sells"] and partial["sells"][0]["sellable_qty"] == 1000
    assert full["sells"][0]["sellable_qty"] == 2000
    assert full["cash_est"] > partial["cash_est"],         "当日买入的不可卖部分不得计入影子现金"
    # 差额 = 那 1000 股按本清单实际采用的参考价计得的市值
    ref = float(partial["sells"][0]["ref_price"])
    assert (full["cash_est"] - partial["cash_est"]) == pytest.approx(1000 * ref, rel=2e-3)
