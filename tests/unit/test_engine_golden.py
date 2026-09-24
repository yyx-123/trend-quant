"""引擎 golden 测试（详设 §9：合成行情逐日快照比对 + T+1/跳空/计息）。

手工算死的期望值——任何撮合/记账改动都会在这里立刻显形。
"""

from __future__ import annotations

from datetime import date

import pytest

from engine import (
    Engine,
    EngineStore,
    ExitOrderIntent,
    OrderIntent,
    StopState,
    TradabilityCard,
)
from engine.profiles import CN_STOCK, MarketProfile

pytestmark = pytest.mark.unit

NO_INTEREST = MarketProfile(
    name="test_no_interest",
    commission_rate=0.0000854,
    commission_min=5.0,
    stamp_tax_sell_stock=0.0005,
    stamp_tax_sell_etf=0.0,
    lot_size=100,
    t_plus=1,
    cash_interest_rate=0.0,
)

D1, D2, D3 = date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)


def _buy(engine, symbol, day, qty, close):
    return engine.buy(
        intent=OrderIntent(symbol=symbol, decision_date=day, intent_type="quantity", value=qty),
        day=day, bar_close=close, card=TradabilityCard(), asset_type="etf",
        slippage_base=0.002, slippage_tail=0.001,
    )


def test_golden_buy_hold_sell_nav():
    """三日产程：D1 买 1000 股@10.00、D2 持有、D3 尾盘卖出@10.50。"""
    engine = Engine(profile=NO_INTEREST, initial_cash=50_000)
    sym = "510300.SS"

    engine.begin_day(day=D1)
    r = _buy(engine, sym, D1, 1000, 10.00)
    assert r.status == "filled"
    assert r.fill.fill_price == pytest.approx(10.03)
    nav1 = engine.settle(day=D1, close_prices={sym: 10.00})
    assert nav1["cash"] == pytest.approx(50_000 - 10_035.0)      # 39965
    assert nav1["equity"] == pytest.approx(39_965 + 10_000)      # 49965
    assert nav1["exposure"] == pytest.approx(10_000 / 49_965)
    # T+1：当日买入不可卖
    pos = engine.account.positions[sym]
    assert pos.quantity == 1000 and pos.sellable_quantity == 0

    # T+1 滚动在次日开盘：D2 begin_day 后才可卖
    engine.begin_day(day=D2)
    assert engine.account.positions[sym].sellable_quantity == 1000
    nav2 = engine.settle(day=D2, close_prices={sym: 10.50})
    assert nav2["equity"] == pytest.approx(39_965 + 10_500)      # 50465

    engine.begin_day(day=D3)
    r = engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D3, fill_mode="tail", source="signal"),
        day=D3, card=TradabilityCard(), asset_type="etf", bar_close=10.50,
        slippage_base=0.002, slippage_tail=0.001,
    )
    assert r.status == "filled"
    assert r.fill.fill_price == pytest.approx(10.4685)           # 10.50 × 0.997
    nav3 = engine.settle(day=D3, close_prices={sym: 10.50})
    assert nav3["cash"] == pytest.approx(39_965 + 10_468.5 - 5.0)  # 50428.5
    assert nav3["equity"] == pytest.approx(50_428.5)
    assert nav3["exposure"] == 0.0


def test_t1_blocks_same_day_sell():
    engine = Engine(profile=NO_INTEREST, initial_cash=50_000)
    sym = "510300.SS"
    assert _buy(engine, sym, D1, 1000, 10.00).status == "filled"
    r = engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D1, fill_mode="tail", source="signal"),
        day=D1, card=TradabilityCard(), asset_type="etf", bar_close=10.10,
        slippage_base=0.002, slippage_tail=0.001,
    )
    assert r.status == "unfilled" and r.unfilled.reason == "t1_block"
    # 次日可卖（begin_day 滚动）
    engine.settle(day=D1, close_prices={sym: 10.10})
    engine.begin_day(day=D2)
    r2 = engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D2, fill_mode="tail", source="signal"),
        day=D2, card=TradabilityCard(), asset_type="etf", bar_close=10.10,
        slippage_base=0.002, slippage_tail=0.001,
    )
    assert r2.status == "filled"


def test_intraday_stop_via_engine_gap_and_touch():
    engine = Engine(profile=NO_INTEREST, initial_cash=50_000)
    sym = "510300.SS"
    engine.begin_day(day=D1)
    _buy(engine, sym, D1, 1000, 10.00)
    engine.settle(day=D1, close_prices={sym: 10.00})
    engine.begin_day(day=D2)
    # 跳空穿透：open 9.2 < stop 9.5 → 按 9.2 成交（无滑点）
    r = engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D2, fill_mode="intraday_stop",
                               stop_price=9.5, source="stop"),
        day=D2, card=TradabilityCard(), asset_type="etf",
        bar_open=9.2, bar_low=9.1, bar_close=9.3,
    )
    assert r.status == "filled" and r.fill.fill_price == pytest.approx(9.2)
    assert engine.account.positions.get(sym) is None

    # 触及按止损价
    engine2 = Engine(profile=NO_INTEREST, initial_cash=50_000)
    engine2.begin_day(day=D1)
    _buy(engine2, sym, D1, 1000, 10.00)
    engine2.settle(day=D1, close_prices={sym: 10.00})
    engine2.begin_day(day=D2)
    r2 = engine2.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D2, fill_mode="intraday_stop",
                               stop_price=9.5, source="stop"),
        day=D2, card=TradabilityCard(), asset_type="etf",
        bar_open=10.0, bar_low=9.4, bar_close=9.6,
    )
    assert r2.status == "filled" and r2.fill.fill_price == pytest.approx(9.5)


def test_cash_interest_accrual():
    """空仓资金固定年化 1%（252 交易日摊提）。252000 元一天利息恰为 10 元。"""
    engine = Engine(profile=CN_STOCK, initial_cash=252_000)
    nav = engine.settle(day=D1, close_prices={})
    assert nav["interest"] == pytest.approx(10.0)
    assert nav["equity"] == pytest.approx(252_010.0)


def test_heat_calculation():
    engine = Engine(profile=NO_INTEREST, initial_cash=100_000)
    sym = "510300.SS"
    engine.buy(
        intent=OrderIntent(symbol=sym, decision_date=D1, intent_type="quantity", value=1000),
        day=D1, bar_close=10.0, card=TradabilityCard(), asset_type="etf",
        slippage_base=0.0, slippage_tail=0.0,
        on_filled=lambda fill: (
            StopState(stop_price=9.4, highest_since_buy=10.0, atr_at_entry=0.2),
            "hard_stop@1",
        ),
    )
    nav = engine.settle(day=D1, close_prices={sym: 10.0})
    # heat = (10.0 − 9.4) × 1000 = 600
    assert nav["heat"] == pytest.approx(600.0)


def test_store_persistence(test_db):
    """落库链路：runs/orders/fills/positions/daily_nav 五表齐全。"""
    store = EngineStore(test_db, "R-golden-1")
    store.begin_run(kind="backtest", strategy_ref="base-v1@1", config_hash="abc",
                    resolved_config_yaml="name: x", data_version=7)
    engine = Engine(profile=NO_INTEREST, initial_cash=50_000, store=store)
    sym = "510300.SS"
    engine.begin_day(day=D1)
    _buy(engine, sym, D1, 1000, 10.00)
    engine.settle(day=D1, close_prices={sym: 10.00})
    engine.begin_day(day=D2)
    engine.sell(
        intent=ExitOrderIntent(symbol=sym, decision_date=D2, fill_mode="tail", source="signal"),
        day=D2, card=TradabilityCard(), asset_type="etf", bar_close=10.50,
        slippage_base=0.002, slippage_tail=0.001,
    )
    engine.settle(day=D2, close_prices={sym: 10.50})
    # 一笔未成交记录（涨停拒买）
    engine.buy(
        intent=OrderIntent(symbol=sym, decision_date=D2, intent_type="quantity", value=500),
        day=D2, bar_close=10.5, card=TradabilityCard(is_limit_up=True), asset_type="etf",
        slippage_base=0.002, slippage_tail=0.001,
    )
    store.finish_run(status="finished")

    run = EngineStore.get_run(test_db, "R-golden-1")
    assert run["status"] == "finished" and run["data_version"] == 7
    fills = EngineStore.load_fills(test_db, "R-golden-1")
    assert len(fills) == 2
    assert fills[0]["fill_price"] == pytest.approx(10.03)
    assert fills[1]["stamp_tax"] == 0.0
    unfilled = EngineStore.load_unfilled(test_db, "R-golden-1")
    assert len(unfilled) == 1 and unfilled[0]["reason"] == "limit_up"
    orders = EngineStore.load_orders(test_db, "R-golden-1")
    assert [o["status"] for o in orders] == ["filled", "filled", "unfilled"]
    nav = EngineStore.load_nav(test_db, "R-golden-1")
    assert len(nav) == 2 and nav[0]["equity"] == pytest.approx(49_965)
    positions = EngineStore.load_positions(test_db, "R-golden-1", day="2024-03-11")
    assert len(positions) == 1
    assert positions[0]["quantity"] == 1000 and positions[0]["sellable_quantity"] == 0
