"""K3-R2 / DS-R2 / GLM53F 三轮复审的单元级回归钉子（删掉实现即失败）。

覆盖：ETF 涨跌停跟随标的板块（GLM53F-P1-1）、新股豁免分板块/分时代
（GLM53F-P2-17）、元模块 prepare_with_gateway 转发（P1-2）与成员参数
schema 校验（P2-19）、live 止损重建 T-1 口径（P1-3）、concentration_cap
真断言（P1-5）、parity 归因价差界限 + unexplained 桶 + nav 长度断言
（DS-R2 §4-4）、小样本 t 临界（P2-1②）、dict 形态 to 的 resolve（P2-7）。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from portfolio.registry import ModuleSpec, fresh_registry
from portfolio.slots import register_builtin_modules

pytestmark = pytest.mark.unit


@pytest.fixture
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


# ----------------------------------------------------------------------
# GLM53F-P1-1：ETF 涨跌停跟随标的板块
# ----------------------------------------------------------------------


def test_board_limit_pct_etf_rules():
    from gateway.tradability import board_limit_pct

    # 股票规则不变
    assert board_limit_pct("600519.SS") == 0.10
    assert board_limit_pct("300750.SZ") == 0.20
    assert board_limit_pct("688981.SS") == 0.20
    # ETF：跟随标的板块
    assert board_limit_pct("159915.SZ", asset_type="etf", name="创业板ETF易方达") == 0.20
    assert board_limit_pct("588000.SS", asset_type="etf", name="科创50ETF") == 0.20
    assert board_limit_pct("510300.SS", asset_type="etf", name="沪深300ETF") == 0.10
    # 旧调用形态兼容（无 asset_type → 按股票代码前缀）
    assert board_limit_pct("510300.SS") == 0.10


def test_etf_limit_up_12pct_chinext_tracker_not_blocked():
    """159915（创业板 ETF）收盘 +12%：真实板内未封板，不得判 is_limit_up。"""
    from gateway.tradability import compute_tradability

    days = list(pd.bdate_range("2024-03-01", periods=3).date)
    closes = pd.Series([10.0, 11.0, 12.32], index=days)  # day3 = +12%
    frame = compute_tradability(
        None, symbols=["159915.SZ"], dates=days,
        raw_closes={"159915.SZ": closes}, ex_factors={},
        listing_dates={"159915.SZ": "2011-09-19"},
        asset_info={"159915.SZ": {"asset_type": "etf", "name": "创业板ETF易方达"}},
    )
    row = frame[frame["date"] == days[2]].iloc[0]
    assert row["limit_up_price"] == pytest.approx(13.20)  # 11.0 × 1.20
    assert not bool(row["is_limit_up"])  # +12% 在 ±20% 板内——旧代码（±10%）会误判

    # 真正封板（+20% 整）仍判涨停
    closes2 = pd.Series([10.0, 11.0, 13.20], index=days)
    frame2 = compute_tradability(
        None, symbols=["159915.SZ"], dates=days,
        raw_closes={"159915.SZ": closes2}, ex_factors={},
        listing_dates={"159915.SZ": "2011-09-19"},
        asset_info={"159915.SZ": {"asset_type": "etf", "name": "创业板ETF易方达"}},
    )
    row2 = frame2[frame2["date"] == days[2]].iloc[0]
    assert bool(row2["is_limit_up"])


# ----------------------------------------------------------------------
# GLM53F-P2-17：新股豁免天数分板块/分时代
# ----------------------------------------------------------------------


def _ipo_frame(symbol, listing, n_days=6, asset_info=None):
    from gateway.tradability import compute_tradability

    days = list(pd.bdate_range(listing, periods=n_days).date)
    closes = pd.Series(np.linspace(10.0, 10.5, n_days), index=days)
    return compute_tradability(
        None, symbols=[symbol], dates=days,
        raw_closes={symbol: closes}, ex_factors={},
        listing_dates={symbol: listing},
        asset_info=asset_info or {},
    ), days


def test_ipo_main_board_old_rule_first_day_only():
    """主板旧规（2023-04-10 前上市）：仅首日无涨跌幅限制，第 2~5 日恢复 ±10%。"""
    frame, days = _ipo_frame("600000.SS", "2020-01-02")
    by_day = {r["date"]: r for _, r in frame.iterrows()}
    assert bool(by_day[days[0]]["no_limit"])                      # 首日
    assert not bool(by_day[days[1]]["no_limit"])                  # 第 2 日起恢复
    assert by_day[days[1]]["limit_up_price"] == pytest.approx(11.0)  # 前收 10.0×1.1
    assert not bool(by_day[days[4]]["no_limit"])                  # 第 5 日也有限制


def test_ipo_main_board_registration_rule_five_days():
    """全面注册制后（2023-04-10 起）主板新股前 5 个交易日无限制。"""
    frame, days = _ipo_frame("601888.SS", "2024-03-04")
    by_day = {r["date"]: r for _, r in frame.iterrows()}
    assert all(bool(by_day[d]["no_limit"]) for d in days[:5])


def test_ipo_etf_has_limits_from_day_one():
    """ETF 上市首日即有限制口径（首日无前收算不出价，第 2 日起限价正常）。"""
    frame, days = _ipo_frame(
        "510500.SS", "2020-01-02",
        asset_info={"510500.SS": {"asset_type": "etf", "name": "中证500ETF"}},
    )
    by_day = {r["date"]: r for _, r in frame.iterrows()}
    assert not any(bool(by_day[d]["no_limit"]) for d in days)     # 全程无豁免
    assert by_day[days[1]]["limit_up_price"] == pytest.approx(11.0)  # 前收 10.0×1.1


# ----------------------------------------------------------------------
# GLM53F-P1-2：元模块 prepare_with_gateway 逐子转发 + P2-19 成员参数校验
# ----------------------------------------------------------------------


def test_meta_signal_forwards_prepare_with_gateway(registry):
    """any_of 包装生产指标类信号：接线必须转发到子模块（否则组合腿静默零信号）。"""
    calls = []

    class _Probe:
        def __init__(self, params):
            self.ready = False

        def prepare_with_gateway(self, bound, symbols, start):
            self.ready = True
            calls.append((tuple(symbols), str(start)))

        def scan(self, ctx, members):
            from portfolio.slots.signal import SignalEvent

            if not self.ready:
                return []
            return [SignalEvent(symbol=m.symbol, kind="entry", date=ctx.date, meta={})
                    for m in members]

    registry.register(ModuleSpec(
        slot="signal", name="probe_gw", version=1, factory=_Probe,
        params_schema={}, kind="builtin", description="probe",
    ))
    meta = registry.require("any_of@1", slot="signal").factory(
        {"members": [{"module": "probe_gw@1"}]}
    )
    assert hasattr(meta, "prepare_with_gateway")  # 顶层 hasattr 探测必须命中
    meta.prepare_with_gateway(object(), ["M00.SS"], "2023-01-02")
    assert calls == [(("M00.SS",), "2023-01-02")]

    # 接线后 scan 才出事件（复刻"不接线的死模块产出 0 事件"场景）
    from types import SimpleNamespace

    ctx = SimpleNamespace(date=date(2023, 1, 3))
    events = meta.scan(ctx, [SimpleNamespace(symbol="M00.SS")])
    assert [e.symbol for e in events] == ["M00.SS"]


def test_meta_member_params_schema_validated(registry):
    """成员参数同样过 schema：atr_mul=-5 包进 members 必须载入即拒。"""
    with pytest.raises(ValueError, match="meta member"):
        registry.require("any_of@1", slot="position_risk").factory(
            {"members": [{"module": "hard_stop@1", "params": {"atr_mul": -5}}]}
        )
    # 合法成员参数照常放行
    ok = registry.require("any_of@1", slot="position_risk").factory(
        {"members": [{"module": "hard_stop@1", "params": {"atr_mul": 1.5}}]}
    )
    assert ok is not None


# ----------------------------------------------------------------------
# GLM53F-P1-3：live 止损重建 T-1 口径
# ----------------------------------------------------------------------


def _live_panel_with_spike():
    """60 日面板：close 线性上行，末根（当日）provisional 且 high 虚高 999。"""
    from gateway.panel import Panel

    n = 60
    days = tuple(pd.bdate_range("2024-01-02", periods=n).date)
    close = 10.0 + 0.1 * np.arange(n)
    close[-1] = 14.5  # 当日回落
    high = close * 1.01
    high[-1] = 999.0  # 当日盘中虚高（provisional spike）
    low = close * 0.99
    data = {
        "open": close.copy(), "high": high, "low": low, "close": close,
        "volume": np.full(n, 1e6), "amount": np.full(n, 1e7),
    }
    provisional = np.zeros((n, 1), dtype=bool)
    provisional[-1, 0] = True  # 末根 = 当日 14:00 合成 bar
    return Panel(dates=days, symbols=("M00.SS",), data={k: v[:, None] for k, v in data.items()},
                 provisional=provisional)


def test_live_stop_rebuild_excludes_provisional_spike(registry):
    """吊灯重建：当日 provisional 虚高不得进 highest/止损价（与回测 T-1 口径一致）。"""
    from core.indicators import atr as core_atr
    from portfolio.live import _rebuild_stop_state

    panel = _live_panel_with_spike()
    module = registry.require("chandelier@1", slot="position_risk").factory(
        {"atr_mul": 2.5, "atr_period": 20}
    )
    module._registered_key = "chandelier@1"
    buy_idx = 40
    buy_date = panel.dates[buy_idx]
    entry_price = float(panel.data["close"][buy_idx, 0])

    state = _rebuild_stop_state(module, panel, "M00.SS", buy_date, entry_price)

    # 期望（独立重算）：highest = max(high[40..58])（末根 999 排除）；
    # stop = highest − 2.5 × ATR(截至第 58 根)
    state_end = len(panel.dates) - 2
    expected_highest = float(np.max(panel.data["high"][buy_idx: state_end + 1, 0]))
    df = pd.DataFrame({
        "high": panel.data["high"][: state_end + 1, 0],
        "low": panel.data["low"][: state_end + 1, 0],
        "close": panel.data["close"][: state_end + 1, 0],
    })
    expected_atr = float(core_atr(df, 20).iloc[-1])
    assert state.highest_since_buy == pytest.approx(expected_highest)
    assert state.highest_since_buy < 100.0  # 999 虚高不得入内
    assert state.stop_price == pytest.approx(expected_highest - 2.5 * expected_atr)


def test_live_stop_rebuild_ratchet_running_max(registry):
    """棘轮重建 = 持仓期逐日候选的累计 max（只上移），且同样排除 provisional 末根。"""
    from core.indicators import atr as core_atr
    from portfolio.live import _rebuild_stop_state

    panel = _live_panel_with_spike()
    module = registry.require("ratchet@1", slot="position_risk").factory(
        {"atr_mul": 2.5, "atr_period": 20}
    )
    module._registered_key = "ratchet@1"
    buy_idx = 40
    state = _rebuild_stop_state(
        module, panel, "M00.SS", panel.dates[buy_idx], float(panel.data["close"][buy_idx, 0])
    )
    state_end = len(panel.dates) - 2
    running_high, expected = 0.0, None
    for d in range(buy_idx, state_end + 1):
        running_high = max(running_high, float(panel.data["high"][d, 0]))
        df = pd.DataFrame({
            "high": panel.data["high"][: d + 1, 0],
            "low": panel.data["low"][: d + 1, 0],
            "close": panel.data["close"][: d + 1, 0],
        })
        atr_d = float(core_atr(df, 20).iloc[-1])
        if atr_d > 0:
            cand = running_high - 2.5 * atr_d
            expected = cand if expected is None else max(expected, cand)
    assert state.stop_price == pytest.approx(expected)
    assert state.highest_since_buy < 100.0


# ----------------------------------------------------------------------
# GLM53F-P1-5：concentration_cap 真断言（复刻无参 instruments() 调用形态）
# ----------------------------------------------------------------------


def test_concentration_cap_blocks_third_same_l2(registry):
    """已有 2 只同类 l2 持仓时，第三只同 l2 被拦；异类放行。"""
    from datetime import datetime, time

    from engine.models import Account, OrderIntent, Position
    from portfolio.context import AccountView, DayContext, PanelView

    panel = _live_panel_with_spike()
    upto = len(panel.dates) - 1
    day = panel.dates[upto]

    class _Meta:
        @staticmethod
        def instruments(wanted=None):
            table = {"M00.SS": "L2A", "M01.SS": "L2A", "M02.SZ": "L2A", "M03.SS": "L2B"}
            pool = tuple(wanted) if wanted else tuple(table)
            return {s: {"category_l2": table.get(s, "")} for s in pool}

    class _GW:
        metadata = _Meta()
        data_version = 1

    account = Account(cash=100_000.0)
    for s in ("M00.SS", "M01.SS"):  # 两只 L2A 持仓
        account.positions[s] = Position(
            symbol=s, quantity=100, sellable_quantity=100, avg_cost=10.0,
            entry_date=day, entry_price=10.0, stop=None,
        )
    close_map = {s: float(panel.data["close"][upto, 0]) for s in panel.symbols}
    ctx = DayContext(
        date=day, as_of=datetime.combine(day, time(15, 0)),
        gateway=_GW(), panel=PanelView(panel, upto),
        account=AccountView(account, close_map),
        params={}, data_version=1, history=[], gate_log=[], extras={},
    )
    gate = registry.require("concentration_cap@1", slot="portfolio_risk").factory({"per_l2": 2})
    intents = [
        OrderIntent(symbol="M02.SZ", decision_date=day, intent_type="quantity",
                    value=100, source="signal"),   # L2A 第三只 → 拦
        OrderIntent(symbol="M03.SS", decision_date=day, intent_type="quantity",
                    value=100, source="signal"),   # L2B → 放行
    ]
    out = gate.admit(ctx, intents, [])
    assert [i.symbol for i in out] == ["M03.SS"]
    assert any(g["gate"] == "concentration_cap" for g in ctx.gate_log)


# ----------------------------------------------------------------------
# DS-R2 §4-4：parity 归因价差界限 + unexplained 桶 + nav 长度断言
# ----------------------------------------------------------------------


def _result(trades, nav, legacy=False):
    if legacy:
        trades = [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                   "exec_price": t["price"]} for t in trades]
    return {"trades": trades, "daily_nav": nav}


def test_parity_tail_slippage_bound_and_unexplained():
    from engine.parity import attribute_diffs

    d = date(2023, 1, 4)
    nav = [{"date": "2023-01-03", "equity": 1.0}, {"date": "2023-01-04", "equity": 1.001}]
    # 价差 0.5%（滑点界限内）→ tail_slippage
    new = _result([{"date": d, "side": "BUY", "qty": 100, "price": 10.05}], nav)
    old = _result([{"date": d, "side": "BUY", "qty": 100, "price": 10.0}], nav, legacy=True)
    out = attribute_diffs(new, old)
    assert out["classified"]["tail_slippage"] == 1
    assert out["unexplained"] == []

    # 价差 5%（超滑点界限）→ unexplained（旧代码任何价差都归白名单）
    new2 = _result([{"date": d, "side": "BUY", "qty": 100, "price": 10.5}], nav)
    out2 = attribute_diffs(new2, old)
    assert out2["classified"]["tail_slippage"] == 0
    assert len(out2["unexplained"]) == 1

    # nav 长度不齐 → unexplained（旧代码 zip 静默截断）
    new3 = _result([], nav + [{"date": "2023-01-05", "equity": 1.002}])
    out3 = attribute_diffs(new3, _result([], nav, legacy=True))
    assert any(u["kind"] == "nav_length_mismatch" for u in out3["unexplained"])


# ----------------------------------------------------------------------
# GLM53F-P2-1②：小样本 t 临界值（不退回 z=1.645）
# ----------------------------------------------------------------------


def test_t_critical_95_small_sample():
    from research.verdict_rules import _t_critical_95

    assert _t_critical_95(2500) == pytest.approx(1.6449, abs=2e-3)  # 大样本 → z
    assert _t_critical_95(30) == pytest.approx(1.697, abs=5e-3)     # t(30) 表值 1.697
    assert _t_critical_95(10) == pytest.approx(1.812, abs=8e-3)     # t(10) 表值 1.812


def test_verdict_gate_uses_t_critical_for_small_samples():
    from research.verdict_rules import suggest_backtest_verdict

    base_evidence = {
        "deltas_vs_base": {"delta_sharpe": 0.4},
        "plateau": None,
        "regime_split": {},
    }
    paired_small = {"t_stat": 1.66, "dsr_on_diff": 0.1, "n_pairs": 31,
                    "band": {"low": 0.01, "high": 0.5}}
    ev_small = {**base_evidence, "stats": {"paired": paired_small}}
    # n=31 → 临界 ≈1.697：t=1.66 不够（旧代码 z=1.645 会放行）
    assert suggest_backtest_verdict(ev_small) == "inconclusive"
    paired_big = {**paired_small, "n_pairs": 2500}
    ev_big = {**base_evidence, "stats": {"paired": paired_big}}
    assert suggest_backtest_verdict(ev_big) == "confirmed"


# ----------------------------------------------------------------------
# GLM53F-P2-7：dict 形态 to 的 resolve 支持
# ----------------------------------------------------------------------


_BASE_YAML = """
name: t
universe: {module: category_filter@1, params: {asset_type: all}}
signal: {module: macd_cross@1, params: {}}
rank: {module: by_freshness@1, params: {}}
sizing: {module: fixed_slots@1, params: {slots: 3}}
portfolio_risk: []
position_risk: {module: hard_stop@1, params: {atr_mul: 1.5}}
execution: {module: tail_session@1, params: {}}
"""


def test_apply_diff_supports_dict_to_form(registry):
    from portfolio.strategy import apply_diff, parse_strategy_yaml

    base = parse_strategy_yaml(_BASE_YAML, registry)
    cfg = apply_diff(base, [{
        "slot": "position_risk", "from": "hard_stop@1",
        "to": {"module": "hard_stop@1", "params": {"atr_mul": 1.0}},
    }], registry)
    binding = cfg.slots["position_risk"]
    assert binding.module == "hard_stop@1"
    assert float(binding.params["atr_mul"]) == 1.0


# ----------------------------------------------------------------------
# GLM53F-P2-4：重复检测的类型逃逸关闭
# ----------------------------------------------------------------------


def test_duplicate_detection_type_escape_closed():
    from research.experiments import _values_similar

    assert _values_similar({"p": 2.0}, {"p": "2.0"})   # 数值字符串 ≡ 数值
    assert _values_similar({"p": True}, {"p": "true"})
    assert _values_similar(2, 2.0)
    assert _values_similar({"p": 2.0}, {"p": "2.1"})   # ±10% 内相似
    assert not _values_similar({"p": 2.0}, {"p": "2.5"})   # 超 ±10% 不相似
    assert not _values_similar({"p": True}, {"p": 1})      # bool ≠ 数值
    assert not _values_similar({"p": 2.0}, {"p": "abc"})   # 非数值字符串不相似
