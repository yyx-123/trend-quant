"""Round 16 修复钉子：部分重建不得冲掉"需全量重建"标记（R16B-B2）。"""

from __future__ import annotations

import pytest


def test_partial_rebuild_does_not_clear_drift_flag(test_db, monkeypatch):
    """R16B-B2：部分重建（冻结后的日更尾/补齐后）不能登记 default 参数集。

    旧行为：任何 `rebuild_all` 都登记 → 漂移标记由 True 变 False → 未被重建的标的
    永远停在旧参数缓存上（实测：未被重建标的 trend_daily 行数 0）。
    """
    from core import run_freeze
    from services import indicator_builder as ib
    from core.strategy_config import get_strategy_config

    cfg = get_strategy_config()
    # 制造"配置漂移"：写入一个与当前不一致的登记
    test_db.save_param_set("default", '{"n_short": 999}', True, 0)
    assert ib.default_param_set_needs_rebuild(cfg, db=test_db) is True

    # 部分重建：不得登记 → 漂移标记必须保持 True
    ib.rebuild_all(["X.SS"], trend_cfg=cfg, db=test_db, partial=True)
    assert ib.default_param_set_needs_rebuild(cfg, db=test_db) is True, \
        "部分重建不得冲掉全量重建标记"

    # 全量重建：登记 → 标记清掉
    ib.rebuild_all(trend_cfg=cfg, db=test_db)
    assert ib.default_param_set_needs_rebuild(cfg, db=test_db) is False


def _account_stub(cash: float = 1_000_000.0):
    from engine.models import Account

    return Account(cash=cash)


def test_star_min_order_qty_is_enforced():
    """R16-D-1：科创板最小申报 200 股——100 股委托不得记账成交。

    真实入口实测（修复前）：`match_buy("688498.SS", bar_close=1886,
    intent_type="target_value", intent_value=200000)` → `filled qty=100`
    （≈18.9 万），而 100 股在科创板必被券商拒单。
    """
    from datetime import date as _date

    from engine.matcher import TradabilityCard, match_buy
    from engine.profiles import CN_STOCK, STAR_MIN_ORDER_QTY, min_buy_qty

    assert min_buy_qty("688498.SS", asset_type="stock") == STAR_MIN_ORDER_QTY == 200
    assert min_buy_qty("600519.SS", asset_type="stock") == 100
    assert min_buy_qty("510300.SS", asset_type="etf") == 100
    day = _date(2026, 7, 6)

    def _buy(symbol, *, intent_type, value, price, asset_type):
        return match_buy(
            order_id="O-1", symbol=symbol, day=day, card=TradabilityCard(),
            bar_close=price, intent_type=intent_type, intent_value=value,
            account=_account_stub(), profile=CN_STOCK, asset_type=asset_type,
            slippage_base=0.002, slippage_tail=0.001,
        )

    # ① 科创板：预算只够 100 股 → 不可下（修复前会成交 100 股）
    r = _buy("688498.SS", intent_type="target_value", value=200_000, price=1886.0,
             asset_type="stock")
    assert r.status == "unfilled" and r.unfilled.reason == "below_min_order", r
    # ② 科创板：数量意图 150 股（≥lot 但 <200）→ 同样不可下
    r2 = _buy("688498.SS", intent_type="quantity", value=150, price=1886.0,
              asset_type="stock")
    assert r2.status == "unfilled" and r2.unfilled.reason == "below_min_order", r2
    # ③ 科创板：预算够 200 股以上 → 正常成交且数量 ≥200
    r3 = _buy("688498.SS", intent_type="target_value", value=500_000, price=1886.0,
              asset_type="stock")
    assert r3.status == "filled" and r3.fill.quantity >= STAR_MIN_ORDER_QTY, r3
    # ④ 非科创板不受影响：100 股照常成交
    r4 = _buy("600519.SS", intent_type="quantity", value=100, price=1680.0,
              asset_type="stock")
    assert r4.status == "filled" and r4.fill.quantity == 100, r4
    # ⑤ 原语义保留：不足一手仍是 lot_rounding
    r5 = _buy("600519.SS", intent_type="quantity", value=27, price=1680.0,
              asset_type="stock")
    assert r5.status == "unfilled" and r5.unfilled.reason == "lot_rounding", r5


@pytest.fixture
def registry():
    from portfolio.registry import fresh_registry
    from portfolio.slots import register_builtin_modules

    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def test_live_buy_list_respects_star_min_order(test_db, registry, monkeypatch):
    """R16-D-1（清单侧）：预算低于最小申报数量时不下单，并留可见 caveat。

    直接打桩 `select_entries` 产出"科创板 + 小预算"的买入意图（确定性），
    验证清单不会下发 <200 股的科创板委托，且跳过原因对操作者可见。
    """
    from datetime import datetime as _dt

    from portfolio.live import generate_daily_list
    from portfolio.seed import seed_default_library

    versions = seed_default_library(test_db, registry)
    user = test_db.create_user("r16_star", "pass12345")

    import numpy as np
    import pandas as pd

    n = 320
    # 恒定价格序列：让"预算 → 股数"的算术可预期（策略信号由上面的 select_entries 打桩提供）
    closes = np.full(n, 1886.0)
    dates = pd.bdate_range("2023-01-02", periods=n)
    bars = pd.DataFrame({
        "time": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 1e6), "amount": closes * 1e6,
    })
    for symbol in ("688498.SS", "LIV000.SS"):
        test_db.save_market_data(symbol, bars, price_mode="qfq")
        test_db.save_market_data(symbol, bars, price_mode="raw")
    test_db.save_instrument_metadata([
        {"symbol": "688498.SS", "name": "科创板样本", "category_l1": "T",
         "category_l2": "T2", "category_l3": "", "enabled": 1, "asset_type": "stock"},
        {"symbol": "LIV000.SS", "name": "对照标的", "category_l1": "T",
         "category_l2": "T2", "category_l3": "", "enabled": 1, "asset_type": "etf"},
    ])

    from engine.models import OrderIntent
    import portfolio.live as live_mod

    def fake_select_entries(ctx, *, modules, events, exited_symbols):
        day = ctx.panel.dates[ctx.panel.upto]
        return [], [
            # 科创板：预算 25 万 / 1886 元 → 整手对齐后 100 股 < 200 最小申报
            # （正是 R16A 复现的形态：修复前会下发并"成交"100 股）
            OrderIntent(symbol="688498.SS", decision_date=day,
                        intent_type="target_value", value=250_000.0),
            # 对照（ETF）：同预算 → 100 股 ≥ 100 → 照常下单
            OrderIntent(symbol="LIV000.SS", decision_date=day,
                        intent_type="target_value", value=250_000.0),
        ]

    monkeypatch.setattr(live_mod, "select_entries", fake_select_entries)
    target = generate_daily_list(
        test_db, strategy_version_id=versions["base-v1"], user_id=user["id"],
        as_of=_dt(2024, 3, 15, 14, 0), initial_capital=150_000,
    )
    buys = {b["symbol"]: b["qty"] for b in target["buys"]}
    assert "688498.SS" not in buys, "科创板 100 股委托不得下发"
    assert buys.get("LIV000.SS") == 100, f"对照 ETF 照常下单：{buys}"
    caveats = " ".join(target.get("caveats") or [])
    assert "最小申报" in caveats, f"被跳过的科创板买单必须留可见 caveat：{caveats}"
