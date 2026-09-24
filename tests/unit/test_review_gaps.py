"""审查驱动的覆盖缺口补测（评审 C 的缺口清单 + B 的语义边界）。

每条测试钉住一个具体行为边界：
- matcher 止损：触发先于阻塞（跌停未触发不记 unfilled；触发日跌停记 limit_down）；
- BoundGateway 全方法的越权拒绝 + 留痕；
- 新股上市 5 日窗口的第 5/6 日边界；
- holdout token 错绑实验的拒绝；
- 止损优先于信号（同日止损触发 + 信号退出 → 只成交一次、原因是止损）；
- 实盘对账卖出侧（应卖已卖 → 价差落账；应卖未卖 → 漏执行清单）；
- DSR 绝对数值锚（盲审独立复算值 0.574009）。
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from engine.matcher import TradabilityCard, match_intraday_stop
from engine.models import Account, Position, StopState
from engine.profiles import CN_STOCK
from gateway.service import Gateway, GatewayViolation
from research.holdout import HoldoutError, check_window, grant_token, set_enforced
from research.sessions import get_or_create_default_human_session

pytestmark = pytest.mark.unit

D = date(2024, 3, 15)


# ----------------------------------------------------------------------
# matcher：触发先于阻塞（详设 §4.2 的 limit_down 是"触发日卖不掉"）
# ----------------------------------------------------------------------


def _pos(sellable=1000) -> Position:
    return Position(symbol="510300.SS", quantity=1000, sellable_quantity=sellable,
                    avg_cost=10.0, entry_date=date(2024, 3, 14), entry_price=10.0,
                    stop=StopState(stop_price=9.5, highest_since_buy=10.0, atr_at_entry=0.2))


def test_stop_limit_down_only_when_triggered():
    # 跌停 + 未触及止损（low 9.8 > 9.5）→ not_triggered，不落 unfilled
    r = match_intraday_stop(order_id="O", symbol="510300.SS", day=D,
                            card=TradabilityCard(is_limit_down=True),
                            bar_open=10.0, bar_low=9.8, stop_price=9.5,
                            position=_pos(), account=Account(cash=0),
                            profile=CN_STOCK, asset_type="etf")
    assert r.status == "not_triggered"
    assert r.unfilled is None
    # 跌停 + 触发（low 9.4 ≤ 9.5）→ unfilled(limit_down)
    r2 = match_intraday_stop(order_id="O", symbol="510300.SS", day=D,
                             card=TradabilityCard(is_limit_down=True),
                             bar_open=10.0, bar_low=9.4, stop_price=9.5,
                             position=_pos(), account=Account(cash=0),
                             profile=CN_STOCK, asset_type="etf")
    assert r2.status == "unfilled" and r2.unfilled.reason == "limit_down"


# ----------------------------------------------------------------------
# BoundGateway：tradability / 生产指标的越权路径也拒绝 + 留痕
# ----------------------------------------------------------------------


def test_bound_gateway_violation_on_all_data_methods(test_db):
    gw = Gateway(test_db)
    bound = gw.bind(as_of=date(2024, 3, 13), caller_layer="research", run_id="R-v")
    with pytest.raises(GatewayViolation):
        bound.get_tradability(symbols=["510300.SS"], dates=[date(2024, 3, 13)],
                              as_of=date(2024, 3, 15))
    with pytest.raises(GatewayViolation):
        bound.get_production_indicator(symbols=["510300.SS"], name="trend_score",
                                       since="2024-01-01", as_of=date(2024, 3, 15))
    gw.flush_audit()
    with test_db.connect() as conn:
        rows = conn.execute(
            "SELECT method FROM gateway_audit WHERE method LIKE 'violation:%' ORDER BY id"
        ).fetchall()
    assert [r["method"] for r in rows] == [
        "violation:get_tradability", "violation:get_production_indicator"
    ]


# ----------------------------------------------------------------------
# 新股 5 日窗口边界（第 5 个交易日内无限制；第 6 个交易日起有限制）
# ----------------------------------------------------------------------


def test_ipo_no_limit_day5_vs_day6():
    from gateway.tradability import compute_tradability

    # 2024-03-04（周一）上市；dates 覆盖两周
    days = pd.bdate_range("2024-03-04", "2024-03-15").date.tolist()
    closes = pd.Series({d: 10.0 for d in days})
    frame = compute_tradability(
        None, symbols=["001234.SZ"], dates=days,
        raw_closes={"001234.SZ": closes}, ex_factors={"001234.SZ": []},
        listing_dates={"001234.SZ": "2024-03-04"},
    )
    by_day = {r["date"]: r for r in frame.to_dict("records")}
    # 上市起 5 个交易日内（3-04 ~ 3-08，含首日共 5 日）无涨跌幅限制
    assert bool(by_day[date(2024, 3, 8)]["no_limit"]) is True
    # 第 6 个交易日（3-11）起有限制
    assert bool(by_day[date(2024, 3, 11)]["no_limit"]) is False
    assert by_day[date(2024, 3, 11)]["limit_up_price"] == 11.0


# ----------------------------------------------------------------------
# holdout token 错绑实验 → 拒绝
# ----------------------------------------------------------------------


def test_holdout_token_bound_to_other_experiment(test_db):
    session = get_or_create_default_human_session(test_db)
    set_enforced(test_db, True)
    try:
        token = grant_token(test_db, session_id=session["session_id"],
                            purpose="绑定 E0001", experiment_id="E0001")
        with pytest.raises(HoldoutError, match="bound to experiment"):
            check_window(test_db, start="2025-01-01", end="2025-06-30",
                         experiment_id="E9999", token_id=token["id"])
        # 绑定实验自己可用
        assert check_window(test_db, start="2025-01-01", end="2025-06-30",
                            experiment_id="E0001", token_id=token["id"]) is True
    finally:
        set_enforced(test_db, False)


def test_holdout_token_single_use_atomic(test_db):
    """TOCTOU：token 消费是原子更新，二次使用必拒。"""
    session = get_or_create_default_human_session(test_db)
    set_enforced(test_db, True)
    try:
        token = grant_token(test_db, session_id=session["session_id"], purpose="一次性")
        assert check_window(test_db, start="2025-01-01", end="2025-06-30",
                            token_id=token["id"]) is True
        with pytest.raises(HoldoutError):
            check_window(test_db, start="2025-01-01", end="2025-06-30",
                         token_id=token["id"])
    finally:
        set_enforced(test_db, False)


# ----------------------------------------------------------------------
# DSR 绝对数值锚（盲审 C 独立复算参考值 0.574009）
# ----------------------------------------------------------------------


def test_dsr_absolute_anchor():
    from research.stats.psr import dsr

    got = dsr(0.08, 500, -0.3, 4.5, 10)
    assert got == pytest.approx(0.5740092653864478, abs=1e-9)


# ----------------------------------------------------------------------
# 止损优先于信号（详设 §5.4.2：先风控后信号）
# ----------------------------------------------------------------------


def test_stop_priority_over_signal_same_day(test_db):
    """止损优先于信号（详设 §5.4.2 先风控后信号）：同日止损触发 + MACD 死叉
    退出并存 → 只成交一次且按止损价（盘中触及价，而不是信号退出的尾盘价）。

    构造：前 40 日温和上行（MACD 金叉早已入场），第 41 日盘中崩落——
    open 10.6（高于止损 10.17，不跳空）/ low 7.2（触及）/ close 7.3：
    止损按 stop_price 成交（10.17），若信号先跑会按尾盘 close 7.3 成交。
    两个价显著不同 → 成交价即执行序的证据。
    """
    from portfolio.backtester import run_backtest
    from portfolio.registry import fresh_registry
    from portfolio.slots import register_builtin_modules
    from portfolio.strategy import parse_strategy_yaml

    reg = fresh_registry()
    register_builtin_modules(reg)
    n = 45
    closes = [10.0 + 0.006 * i for i in range(40)]          # 温和上行
    opens = list(closes)
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    # 第 41 日：跳空触及（open 10.1 < 止损 10.14 → 跳空按 open 成交；
    # close 9.7 远离——若信号先跑则按尾盘 9.7 成交，两价可区分）。
    # 幅度控制在跌停线内（-5.3%），避免跌停卡控的顺延语义干扰本测试主题。
    opens.append(10.1)
    highs.append(10.15)
    lows.append(9.5)
    closes.append(9.7)
    # 尾部几日低位横盘（防重生金叉干扰断言）
    for _ in range(4):
        opens.append(9.7)
        highs.append(9.72)
        lows.append(9.68)
        closes.append(9.7)
    dates = pd.bdate_range("2023-01-02", periods=n)
    df = pd.DataFrame({
        "time": dates, "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 2e6), "amount": np.full(n, 2e7),
    })
    test_db.save_market_data("AAA001.SS", df, price_mode="qfq")
    test_db.save_market_data("AAA001.SS", df, price_mode="raw")
    test_db.save_instrument_metadata([{
        "symbol": "AAA001.SS", "name": "A", "category_l1": "T", "category_l2": "T2",
        "category_l3": "", "enabled": 1, "asset_type": "etf",
    }])
    cfg = parse_strategy_yaml("""
name: t-stop-priority
universe: {module: static_list@1, params: {symbols: [AAA001.SS]}}
signal: {module: macd_cross@1, params: {use_exit: true, valid_days: 60}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 1}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {slippage_base: 0.0, slippage_tail: 0.0}}
""", reg)
    result = run_backtest(
        test_db, config=cfg, registry=reg,
        start=dates[1].date(), end=dates[-1].date(),
        initial_capital=100_000, store=False,
    )
    sells = [t for t in result["trades"] if t["side"] == "sell"]
    assert len(sells) == 1, "同日止损+死叉只应成交一次"
    fill_price = sells[0]["price"]
    assert fill_price == pytest.approx(10.1, abs=0.02), (
        f"应按止损跳空开盘价成交（10.1），实际 {fill_price}——"
        "若按尾盘 close 9.7 成交则信号先于止损（顺序错了）"
    )
    assert abs(fill_price - 9.7) > 0.3  # 明确不是尾盘口径


# ----------------------------------------------------------------------
# 实盘对账卖出侧
# ----------------------------------------------------------------------


def test_live_reconcile_sell_side(test_db):
    """应卖已卖 → 价差落账（卖出价差为负=卖得比基准低）；应卖未卖 → missing。"""
    import json
    from datetime import date as _date

    test_db.create_user("u1", "pass12345")
    user_id = 1
    target = {
        "as_of": "2024-03-14 14:00:00", "strategy_version_id": "base-v1@1",
        "sells": [{"symbol": "AAA001.SS", "qty": 1000, "reason": "hard_stop",
                   "ref_price": 10.0, "est_price": 9.97}],
        "buys": [], "target_holdings": [], "gate_log": [],
    }
    with test_db.connect() as conn:
        conn.execute(
            """INSERT INTO portfolio_live_lists
               (list_date, strategy_version_id, as_of, target_json)
               VALUES (?, ?, ?, ?)""",
            ("2024-03-14", "base-v1@1", "2024-03-14 14:00:00",
             json.dumps(target, ensure_ascii=False)),
        )
    # 用户按 9.90 卖出（低于收盘基准 10.00 → 执行差 −1.0%；GLM53F-P2-18：
    # 对账分母 = ref_price 收盘基准价，不再是含模型滑点的 est_price）
    trade = test_db.create_manual_trade(user_id, "AAA001.SS", "2024-03-01", 10.5, 1000)
    test_db.close_manual_trade(trade["id"], "2024-03-14", 9.90)

    from portfolio.live import reconcile_daily_list

    rec = reconcile_daily_list(test_db, list_date="2024-03-14",
                               strategy_version_id="base-v1@1", user_id=user_id)
    assert rec["missing_sells"] == []
    assert len(rec["price_diffs"]) == 1
    assert rec["price_diffs"][0]["side"] == "sell"
    # reconcile 的 diff_pct 按 5 位小数落库（round(..., 5)），容差覆盖该舍入
    assert rec["price_diffs"][0]["bench_price"] == 10.0
    assert rec["price_diffs"][0]["diff_pct"] == pytest.approx(9.90 / 10.0 - 1.0, abs=1e-5)

    # 另一日：清单应卖但用户未卖 → missing_sells
    target2 = {**target, "sells": [{"symbol": "BBB002.SS", "qty": 500,
                                    "reason": "chandelier", "ref_price": 5.0,
                                    "est_price": 4.985}]}
    with test_db.connect() as conn:
        conn.execute(
            """INSERT INTO portfolio_live_lists
               (list_date, strategy_version_id, as_of, target_json)
               VALUES (?, ?, ?, ?)""",
            ("2024-03-15", "base-v1@1", "2024-03-15 14:00:00",
             json.dumps(target2, ensure_ascii=False)),
        )
    rec2 = reconcile_daily_list(test_db, list_date="2024-03-15",
                                strategy_version_id="base-v1@1", user_id=user_id)
    assert rec2["missing_sells"] == ["BBB002.SS"]


def test_ipo_before_window_does_not_disable_limits():
    """K3-P2-1/DS-P1-1：上市日早于窗口起点时，老股在窗口首日就应有涨跌停限制
    （不再按窗口内第几天误判新股）。"""
    from gateway.tradability import compute_tradability

    days = pd.bdate_range("2024-03-04", "2024-03-08").date.tolist()
    # raw closes 含窗口前一日（垫片）——真实调用形态（回测/live 都带前收）
    closes = pd.Series({date(2024, 3, 1): 10.0, **{d: 10.0 for d in days}})
    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=days,
        raw_closes={"600519.SS": closes}, ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": "2001-08-27"},  # 老股
    )
    assert len(frame) == 5
    for r in frame.to_dict("records"):
        assert bool(r["no_limit"]) is False
        assert r["limit_up_price"] == 11.0
        assert r["is_limit_up"] is False


def test_live_single_day_form_has_limits():
    """live 形态（单日 dates）下涨跌停推导不失效。"""
    from gateway.tradability import compute_tradability

    frame = compute_tradability(
        None, symbols=["600519.SS"], dates=[date(2024, 3, 15)],
        raw_closes={"600519.SS": pd.Series({date(2024, 3, 14): 10.0, date(2024, 3, 15): 11.0})},
        ex_factors={"600519.SS": []},
        listing_dates={"600519.SS": "2001-08-27"},
    )
    row = frame.iloc[0]
    assert row["limit_up_price"] == 11.0
    assert bool(row["is_limit_up"]) is True
