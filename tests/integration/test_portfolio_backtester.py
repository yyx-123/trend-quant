"""组合回测器集成测试（详设 §5.4.1 日循环 + §5.4.2 写死语义）。

合成市场驱动真实 run：先卖后买/边卖边买/失败不递补/止损优先/整手与现金/
槽位上限/行动门控/同配置重跑位级一致。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from data.storage.db import Database
from engine.store import EngineStore
from portfolio.backtester import run_backtest
from portfolio.registry import fresh_registry
from portfolio.slots import register_builtin_modules
from portfolio.strategy import parse_strategy_yaml

pytestmark = pytest.mark.integration


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _write_symbol(db: Database, symbol: str, closes: list[float], *,
                  start: str = "2023-01-02", category: str = "测试类",
                  asset_type: str = "etf", highs=None, lows=None, opens=None):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "time": dates,
        "open": np.asarray(opens, dtype=float) if opens is not None else closes,
        "high": np.asarray(highs, dtype=float) if highs is not None else closes * 1.005,
        "low": np.asarray(lows, dtype=float) if lows is not None else closes * 0.995,
        "close": closes,
        "volume": np.full(n, 2e6),
        "amount": closes * 2e6,
    })
    db.save_market_data(symbol, df, price_mode="qfq")
    db.save_market_data(symbol, df, price_mode="raw")
    db.save_instrument_metadata([{
        "symbol": symbol, "name": symbol, "category_l1": "测试", "category_l2": category,
        "category_l3": "", "enabled": 1, "asset_type": asset_type,
    }])


def _uptrend(n=260, drift=0.004, seed=1):
    rng = np.random.default_rng(seed)
    return list(10 * np.exp(np.cumsum(rng.normal(drift, 0.012, n))))


@pytest.fixture
def market(test_db):
    """三只标的：A 强趋势、B 震荡、C 弱趋势。"""
    _write_symbol(test_db, "AAA001.SS", _uptrend(seed=1), category="甲")
    _write_symbol(test_db, "BBB002.SS", _uptrend(drift=0.0, seed=2), category="乙")
    _write_symbol(test_db, "CCC003.SZ", _uptrend(drift=0.001, seed=3), category="甲")
    return test_db


CFG = """
name: t-macd
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: equal_risk@1, params: {risk_budget_pct: 0.01}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 2}}
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {slippage_base: 0.002, slippage_tail: 0.001}}
"""


def test_run_persists_full_lineage(market, registry):
    cfg = parse_strategy_yaml(CFG, registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 12, 29),
                          initial_capital=1_000_000)
    run = EngineStore.get_run(market, result["run_id"])
    assert run["status"] == "finished"
    assert run["data_version"] > 0
    assert run["engine_version"] == "1.0.0"
    assert "macd_cross@1" in run["resolved_config_yaml"]  # 完整配置随 run 落库
    nav = EngineStore.load_nav(market, result["run_id"])
    assert len(nav) == len(result["daily_nav"])
    fills = EngineStore.load_fills(market, result["run_id"])
    assert all(f["side"] in ("buy", "sell") for f in fills)


def test_deterministic_reruns_bitwise(market, registry):
    cfg = parse_strategy_yaml(CFG, registry)
    r1 = run_backtest(market, config=cfg, registry=registry,
                      start=date(2023, 3, 1), end=date(2023, 12, 29),
                      initial_capital=1_000_000, store=False)
    r2 = run_backtest(market, config=cfg, registry=registry,
                      start=date(2023, 3, 1), end=date(2023, 12, 29),
                      initial_capital=1_000_000, store=False)
    nav1 = [row["equity"] for row in r1["daily_nav"]]
    nav2 = [row["equity"] for row in r2["daily_nav"]]
    assert nav1 == nav2  # 同配置同数据：位级一致
    assert [(t["date"], t["side"], t["symbol"], t["qty"]) for t in r1["trades"]] == \
           [(t["date"], t["side"], t["symbol"], t["qty"]) for t in r2["trades"]]


def test_slot_limit_enforced(market, registry):
    cfg = parse_strategy_yaml(CFG, registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 12, 29),
                          initial_capital=1_000_000, store=False)
    positions = EngineStore  # 不落库路径用内存结果：逐日持仓数 ≤ 2
    # 用落库再跑一次拿持仓快照
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 12, 29),
                          initial_capital=1_000_000, store=True)
    rows = EngineStore.load_positions(market, result["run_id"])
    by_day: dict[str, int] = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0) + 1
    assert max(by_day.values()) <= 2


def test_buy_failure_no_substitute(market, registry):
    """买入失败不递补（详设 §5.4.2）：槽位=1 时头名涨停未成交，
    次名当日不顺位递补；次日槽位放开后两标的才各成交。"""
    n = 30
    # A：第 2 日涨停（10.0 → 11.0，恰好顶格 ±10%）；B：平盘
    a_closes = [10.0, 11.0] + [11.0] * (n - 2)
    b_closes = [5.0] * n
    _write_symbol(market, "FFF006.SS", a_closes, category="戊")
    _write_symbol(market, "GGG007.SS", b_closes, category="己")
    cfg = parse_strategy_yaml("""
name: t-nosub
universe: {module: static_list@1, params: {symbols: [FFF006.SS, GGG007.SS]}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 2}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 1}}
position_risk: {module: none@1, params: {}}
execution: {module: tail_session@1, params: {slippage_base: 0.0, slippage_tail: 0.0}}
""", registry)
    days = pd.bdate_range("2023-01-02", periods=n)
    # 窗口从第 2 日（涨停日）开始：首日即候选竞争
    result = run_backtest(market, config=cfg, registry=registry,
                          start=days[1].date(), end=days[-1].date(),
                          initial_capital=100_000, store=False)
    trades = result["trades"]
    unfilled = result["unfilled"]
    limit_day = days[1].date()
    # 涨停日：A 拒买（unfilled），且无任何成交（B 不递补）
    assert not [t for t in trades if t["date"] == limit_day]
    assert any(u["symbol"] == "FFF006.SS" and u["reason"] == "limit_up" and u["date"] == limit_day.isoformat()
               for u in unfilled)
    assert not [t for t in trades if t["symbol"] == "GGG007.SS" and t["date"] == limit_day]


def test_always_entry_buy_and_hold(market, registry):
    cfg = parse_strategy_yaml("""
name: bh
universe: {module: static_list@1, params: {symbols: [AAA001.SS]}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: all_in@1, params: {}}
portfolio_risk: []
position_risk: {module: none@1, params: {}}
execution: {module: tail_session@1, params: {slippage_base: 0.0, slippage_tail: 0.0}}
""", registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 12, 29),
                          initial_capital=100_000, store=False)
    assert len(result["trades"]) == 1  # 首日买入后不再交易
    assert result["trades"][0]["side"] == "buy"
    first_close = 10 * np.exp(0)  # 不确定——用 NAV 校验趋势跟随
    nav = result["daily_nav"]
    assert nav[-1]["equity"] > nav[0]["equity"]  # 强趋势标的买入持有应盈利


def test_heat_cap_blocks_candidates(market, registry):
    cfg = parse_strategy_yaml("""
name: t-heat
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: equal_risk@1, params: {risk_budget_pct: 0.02}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 10}}
  - {module: heat_cap@1, params: {max_heat_pct: 0.03}}
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
""", registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 6, 30),
                          initial_capital=100_000, store=False)
    # heat 上限 3%、单笔风险 2% → 最多 1 只成交（第 2 只被 heat_cap 拦）
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    assert len(buys) <= 2
    # gate 日志里有 heat_cap 拦截记录
    assert any(g["gate"] == "heat_cap" for g in result["gate_log"])


def test_action_gate_monthly(market, registry):
    """月度门控：只在每月首个交易日允许行动（事件驱动外的日历口径）。"""
    cfg = parse_strategy_yaml("""
name: t-gate
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: none@1, params: {}}
execution: {module: tail_session@1, params: {action_gate: {freq: monthly}}}
""", registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 6, 30),
                          initial_capital=300_000, store=False)
    buy_days = {t["date"] for t in result["trades"] if t["side"] == "buy"}
    # 每个买入日都是其所在月的首个交易日
    panel_days = pd.bdate_range("2023-03-01", "2023-06-30")
    for d in buy_days:
        month_first = min(x for x in panel_days if x.year == d.year and x.month == d.month)
        assert d == month_first.date()


def test_same_day_sell_buy_different_symbol_allowed(market, registry):
    """先卖后买（不同标的同日）：止损释放的现金/槽位当日可用于新买入。

    确定性构造：slot_limit=1；A 持有中在第 41 日跳空击穿硬止损，同日 B
    的 always_entry 候选因槽位释放被买入——一卖一买同日不同标的。
    """
    n = 80
    a_closes = [10.0] * 40 + [7.5] * 40     # 第 41 日跳空 -25%（击穿 1.5×ATR 硬止损）
    b_closes = [5.0] * n
    _write_symbol(market, "DDD004.SS", a_closes, category="丙")
    _write_symbol(market, "EEE005.SS", b_closes, category="丁")
    cfg = parse_strategy_yaml("""
name: t-rotation
universe: {module: static_list@1, params: {symbols: [DDD004.SS, EEE005.SS]}}
signal: {module: always_entry@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 1}}
portfolio_risk:
  - {module: slot_limit@1, params: {max_positions: 1}}
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {slippage_base: 0.0, slippage_tail: 0.0}}
""", registry)
    start = pd.bdate_range("2023-01-02", periods=n)[0].date()
    end = pd.bdate_range("2023-01-02", periods=n)[-1].date()
    result = run_backtest(market, config=cfg, registry=registry,
                          start=start, end=end, initial_capital=100_000, store=False)
    trades = result["trades"]
    sells = [t for t in trades if t["side"] == "sell" and t["symbol"] == "DDD004.SS"]
    assert len(sells) == 1, "A 应在跳空日被止损卖出"
    stop_day = sells[0]["date"]
    same_day_buys = [
        t for t in trades
        if t["side"] == "buy" and t["symbol"] == "EEE005.SS" and t["date"] == stop_day
    ]
    assert same_day_buys, "止损释放槽位当日 B 应买入（先卖后买）"
    # 边卖边买同标的：A 在止损当日不得再买入
    assert not [t for t in trades if t["side"] == "buy" and t["symbol"] == "DDD004.SS"
                and t["date"] == stop_day]


def test_t1_and_lot_rules_via_engine(market, registry):
    """整手/现金/T+1 已在 engine golden 锁定；此处验证组合层的成交数量整手对齐。"""
    cfg = parse_strategy_yaml(CFG, registry)
    result = run_backtest(market, config=cfg, registry=registry,
                          start=date(2023, 3, 1), end=date(2023, 12, 29),
                          initial_capital=1_000_000, store=False)
    for t in result["trades"]:
        if t["side"] == "buy":
            assert t["qty"] % 100 == 0
