"""L2 费用与撮合单元测试（详设 §9 测试策略：fees 边界 + matcher 全分支）。"""

from __future__ import annotations

from datetime import date

import pytest

from engine.fees import buy_cost, commission, max_affordable_quantity, sell_proceeds
from engine.matcher import (
    TradabilityCard,
    match_buy,
    match_intraday_stop,
    match_tail_sell,
)
from engine.models import Account, Position, StopState
from engine.profiles import CN_STOCK

pytestmark = pytest.mark.unit

D = date(2024, 3, 15)


# ----------------------------------------------------------------------
# fees
# ----------------------------------------------------------------------


def test_commission_min_floor():
    # 万 0.854 × 10000 = 0.854 < 5 → 最低 5 元
    assert commission(10_000, CN_STOCK) == 5.0
    # 大额按费率：1,000,000 × 0.0000854 = 85.4
    assert commission(1_000_000, CN_STOCK) == pytest.approx(85.4)


def test_buy_cost_no_stamp_tax():
    out = buy_cost(base_price=10.0, slippage_base=0.002, slippage_tail=0.001,
                   quantity=1000, profile=CN_STOCK, asset_type="stock")
    assert out["fill_price"] == pytest.approx(10.03)
    assert out["gross"] == pytest.approx(10030.0)
    assert out["commission"] == 5.0
    assert out["stamp_tax"] == 0.0
    assert out["total_cost"] == pytest.approx(10035.0)


def test_sell_stamp_tax_stock_vs_etf():
    stock = sell_proceeds(reference_price=10.0, slippage_base=0.002, slippage_tail=0.001,
                          quantity=1000, profile=CN_STOCK, asset_type="stock")
    assert stock["fill_price"] == pytest.approx(9.97)
    assert stock["stamp_tax"] == pytest.approx(9.97 * 1000 * 0.0005)  # 0.05%
    etf = sell_proceeds(reference_price=10.0, slippage_base=0.002, slippage_tail=0.001,
                        quantity=1000, profile=CN_STOCK, asset_type="etf")
    assert etf["stamp_tax"] == 0.0


def test_sell_no_slippage_for_stop_fill():
    out = sell_proceeds(reference_price=9.5, slippage_base=0.0, slippage_tail=0.0,
                        quantity=100, profile=CN_STOCK, asset_type="etf",
                        apply_slippage=False)
    assert out["fill_price"] == 9.5


def test_max_affordable_quantity_lot_decrement():
    # 现金 10030：1000 股 × 10.03 + 佣金 5 = 10035 > 10030 → 退到 900 股
    qty = max_affordable_quantity(cash=10_030, exec_price_est=10.03, profile=CN_STOCK)
    assert qty == 900
    assert max_affordable_quantity(cash=500, exec_price_est=10.03, profile=CN_STOCK) == 0


# ----------------------------------------------------------------------
# match_buy
# ----------------------------------------------------------------------


def _account(cash=100_000.0) -> Account:
    return Account(cash=cash)


def test_buy_rejected_on_limit_up():
    acc = _account()
    r = match_buy(order_id="O-1", symbol="510300.SS", day=D,
                  card=TradabilityCard(is_limit_up=True), bar_close=10.0,
                  intent_type="quantity", intent_value=1000, account=acc,
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "unfilled" and r.unfilled.reason == "limit_up"
    assert acc.cash == 100_000.0


def test_buy_rejected_when_suspended():
    r = match_buy(order_id="O-1", symbol="510300.SS", day=D,
                  card=TradabilityCard(suspended=True), bar_close=10.0,
                  intent_type="quantity", intent_value=1000, account=_account(),
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.unfilled.reason == "suspended"


def test_buy_quantity_lot_rounding():
    # 等风险算出 27 股 → 不足一手 → lot_rounding（详设 §5.13 示例的"贵到买不起一手"）
    r = match_buy(order_id="O-1", symbol="600519.SS", day=D,
                  card=TradabilityCard(), bar_close=1680.0,
                  intent_type="quantity", intent_value=27, account=_account(),
                  profile=CN_STOCK, asset_type="stock",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "unfilled" and r.unfilled.reason == "lot_rounding"


def test_buy_insufficient_cash_decrement_to_zero():
    # 现金逐手递减后仍能买 400 股 → 成交（设计行为：递减不是拒绝）
    r = match_buy(order_id="O-1", symbol="510300.SS", day=D,
                  card=TradabilityCard(), bar_close=10.0,
                  intent_type="quantity", intent_value=10_000, account=_account(cash=5_000),
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "filled" and r.fill.quantity == 400
    # 现金买不起一手（100 股 × 10.03 + 5 元 > 500）→ insufficient_cash
    r = match_buy(order_id="O-2", symbol="510300.SS", day=D,
                  card=TradabilityCard(), bar_close=10.0,
                  intent_type="quantity", intent_value=10_000, account=_account(cash=500),
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "unfilled" and r.unfilled.reason == "insufficient_cash"


def test_buy_fill_numbers():
    acc = _account(cash=50_000)
    r = match_buy(order_id="O-1", symbol="510300.SS", day=D,
                  card=TradabilityCard(), bar_close=10.0,
                  intent_type="quantity", intent_value=1000, account=acc,
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "filled"
    f = r.fill
    assert f.fill_price == pytest.approx(10.03)
    assert f.quantity == 1000
    assert f.commission == 5.0 and f.stamp_tax == 0.0
    assert f.cash_after == pytest.approx(50_000 - 10_035.0)
    assert r.cash_delta == pytest.approx(-10_035.0)


def test_buy_target_value():
    acc = _account(cash=50_000)
    r = match_buy(order_id="O-1", symbol="510300.SS", day=D,
                  card=TradabilityCard(), bar_close=10.0,
                  intent_type="target_value", intent_value=20_000, account=acc,
                  profile=CN_STOCK, asset_type="etf",
                  slippage_base=0.002, slippage_tail=0.001)
    assert r.status == "filled"
    # 20000 / 10.03 = 1994 → 1900 股
    assert r.fill.quantity == 1900


# ----------------------------------------------------------------------
# match_tail_sell
# ----------------------------------------------------------------------


def _position(qty=1000, sellable=1000) -> Position:
    return Position(
        symbol="510300.SS", quantity=qty, sellable_quantity=sellable,
        avg_cost=10.03, entry_date=date(2024, 3, 14), entry_price=10.03,
        stop=StopState(stop_price=9.5, highest_since_buy=10.5, atr_at_entry=0.2),
    )


def test_tail_sell_blocked_on_limit_down():
    r = match_tail_sell(order_id="O-2", symbol="510300.SS", day=D,
                        card=TradabilityCard(is_limit_down=True), bar_close=10.0,
                        position=_position(), account=_account(), profile=CN_STOCK,
                        asset_type="etf", slippage_base=0.002, slippage_tail=0.001)
    assert r.unfilled.reason == "limit_down"


def test_tail_sell_t1_block():
    r = match_tail_sell(order_id="O-2", symbol="510300.SS", day=D,
                        card=TradabilityCard(), bar_close=10.0,
                        position=_position(sellable=0), account=_account(),
                        profile=CN_STOCK, asset_type="etf",
                        slippage_base=0.002, slippage_tail=0.001)
    assert r.unfilled.reason == "t1_block"


def test_tail_sell_fill_stock_stamp_tax():
    acc = _account(cash=1_000)
    r = match_tail_sell(order_id="O-2", symbol="600519.SS", day=D,
                        card=TradabilityCard(), bar_close=10.0,
                        position=_position(), account=acc, profile=CN_STOCK,
                        asset_type="stock", slippage_base=0.002, slippage_tail=0.001)
    f = r.fill
    assert f.fill_price == pytest.approx(9.97)
    assert f.stamp_tax == pytest.approx(9.97 * 1000 * 0.0005)
    assert f.commission == 5.0
    assert f.cash_after == pytest.approx(1_000 + (9_970 - 5 - 4.985))


# ----------------------------------------------------------------------
# match_intraday_stop
# ----------------------------------------------------------------------


def test_stop_not_triggered_no_unfilled():
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(), bar_open=10.2, bar_low=9.8,
                            stop_price=9.5, position=_position(), account=_account(),
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "not_triggered"
    assert r.unfilled is None


def test_stop_touch_fills_at_stop_price():
    acc = _account(cash=0)
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(), bar_open=10.0, bar_low=9.4,
                            stop_price=9.5, position=_position(), account=acc,
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "filled"
    assert r.fill.fill_price == pytest.approx(9.5)  # 触及价成交，无滑点
    assert r.fill.base_price == 9.5


def test_stop_gap_through_fills_at_open():
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(), bar_open=9.2, bar_low=9.1,
                            stop_price=9.5, position=_position(), account=_account(),
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "filled"
    assert r.fill.fill_price == pytest.approx(9.2)  # 跳空穿透按开盘价成交
    assert r.fill.base_price == 9.2


def test_stop_blocked_at_limit_down_records_unfilled():
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(is_limit_down=True),
                            bar_open=9.0, bar_low=9.0,
                            stop_price=9.5, position=_position(), account=_account(),
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "unfilled" and r.unfilled.reason == "limit_down"
    assert r.unfilled.intent_snapshot["stop_price"] == 9.5


def test_stop_blocked_by_t1_records_unfilled():
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(), bar_open=10.0, bar_low=9.4,
                            stop_price=9.5, position=_position(sellable=0),
                            account=_account(), profile=CN_STOCK, asset_type="etf")
    assert r.status == "unfilled" and r.unfilled.reason == "t1_block"


def test_stop_suspended_records_unfilled():
    r = match_intraday_stop(order_id="O-3", symbol="510300.SS", day=D,
                            card=TradabilityCard(suspended=True),
                            bar_open=None, bar_low=None,
                            stop_price=9.5, position=_position(), account=_account(),
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "unfilled" and r.unfilled.reason == "suspended"
