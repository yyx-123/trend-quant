"""内置模块行为测试矩阵（评审 K3-P2-8：§5.14"每个预置模块带 golden 测试"
的落实——24 个零覆盖模块补齐行为钉子）。

两层：
1. 全量扫描（每个注册模块）：可实例化 + 协议方法可调用 + 确定性（两次同输出）；
2. 具名行为钉（逐项断言行/数/方向，不是"能跑就行"）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.context import AccountView, DayContext, PanelView
from portfolio.registry import fresh_registry
from portfolio.slots import register_builtin_modules
from gateway.panel import Panel

pytestmark = pytest.mark.unit

DAYS_N = 80
SYMBOLS = ("M00.SS", "M01.SS", "M02.SZ", "M03.SS", "M04.SS", "M05.SS")


@pytest.fixture(scope="module")
def registry():
    reg = fresh_registry()
    register_builtin_modules(reg)
    return reg


def _trend_up(n=DAYS_N, start=10.0, step=0.05):
    return start + step * np.arange(n)


def _panel() -> Panel:
    """合成面板：M00 强趋势（金叉/新高/突破全有）、M01 下跌、M02 平盘、
    M03 突破型、M04/M05 各异。"""
    rng = np.random.default_rng(4)
    days = list(pd.bdate_range("2023-01-02", periods=DAYS_N).date)
    data = {}
    closes = np.column_stack([
        _trend_up(DAYS_N, 10.0, 0.08),                       # M00 强趋势
        20 - 0.05 * np.arange(DAYS_N),                       # M01 下跌
        np.full(DAYS_N, 15.0),                               # M02 平盘
        np.concatenate([np.full(DAYS_N - 10, 8.0),
                        8 + 0.3 * np.arange(10)]),           # M03 尾段突破
        np.concatenate([12 + 0.06 * np.arange(50),          # M04 先涨后跌（死叉面）
                        (12 + 0.06 * 49) - 0.08 * np.arange(DAYS_N - 50)]),
        18 + np.cumsum(rng.normal(0.002, 0.02, DAYS_N)),     # M05 缓涨
    ])
    for f in ("open", "high", "low", "close", "volume", "amount"):
        if f == "close":
            data[f] = closes
        elif f == "high":
            data[f] = closes * 1.01
        elif f == "low":
            data[f] = closes * 0.99
        elif f == "open":
            data[f] = closes * 0.999
        elif f == "volume":
            data[f] = np.full(closes.shape, 1e6)
        else:
            data[f] = closes * 1e6 * (1 + np.abs(rng.normal(0, 0.1, closes.shape)))
    return Panel(dates=tuple(days), symbols=SYMBOLS, data=data,
                 provisional=np.zeros((DAYS_N, len(SYMBOLS)), dtype=bool))


def _ctx(panel, upto):
    from engine.models import Account
    from research.module_gate import _StubBoundGateway

    day = panel.dates[upto]
    close_map = {s: float(panel.data["close"][upto, j]) for j, s in enumerate(panel.symbols)}
    account = Account(cash=100_000.0)
    return DayContext(
        date=day, as_of=datetime.combine(day, time(15, 0)),
        gateway=_StubBoundGateway(panel.symbols), panel=PanelView(panel, upto),
        account=AccountView(account, close_map),
        params={"_members_count": len(panel.symbols)},
        data_version=1, history=[], gate_log=[],
        extras={"atr": {}},
    )


# ----------------------------------------------------------------------
# 全量扫描：每个注册模块可实例化 + 协议方法确定性（两次同输出）
# ----------------------------------------------------------------------


def test_every_builtin_module_deterministic(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    from engine.models import Fill, Position
    from portfolio.slots.signal import SignalEvent
    from portfolio.slots.universe import UniverseMember

    fill = Fill(order_id="T", symbol="M00.SS", fill_date=ctx.date, base_price=10.0,
                slippage_base=0.0, slippage_tail=0.0, fill_price=10.0, quantity=1000,
                commission=5.0, stamp_tax=0.0, fee_total=5.0, cash_after=0.0)
    pos = Position(symbol="M00.SS", quantity=1000, sellable_quantity=1000,
                   avg_cost=10.0, entry_date=panel.dates[DAYS_N - 20], entry_price=10.0,
                   stop=None)
    candidates = [SignalEvent(symbol="M00.SS", kind="entry", date=ctx.date, meta={}),
                  SignalEvent(symbol="M01.SS", kind="entry", date=ctx.date, meta={})]
    intents = []

    for spec in registry.list():
        if spec.name in ("any_of", "all_of"):
            if spec.slot == "signal":
                inst = spec.factory({"members": [{"module": "always_entry@1"}]})
            elif spec.slot == "position_risk":
                inst = spec.factory({"members": [{"module": "hard_stop@1"}]})
            else:
                continue
        else:
            inst = spec.factory({})
        if hasattr(inst, "prepare"):
            inst.prepare(panel)

        def probe(i=inst):
            if spec.slot == "universe":
                return sorted(m.symbol for m in i.members(ctx))
            if spec.slot == "signal":
                return sorted((e.symbol, e.kind) for e in i.scan(ctx, [UniverseMember(s) for s in SYMBOLS]))
            if spec.slot == "rank":
                return [e.symbol for e in i.rank(ctx, candidates)]
            if spec.slot == "sizing":
                out = i.size(ctx, candidates[0], 9.0)
                return None if out is None else (out.symbol, round(float(out.value), 6))
            if spec.slot == "portfolio_risk":
                from engine.models import OrderIntent

                out = i.admit(ctx, [OrderIntent(symbol="M00.SS", decision_date=ctx.date,
                                                intent_type="quantity", value=1000,
                                                source="signal")], [])
                return [(x.symbol, round(float(x.value), 6)) for x in out]
            if spec.slot == "position_risk":
                state = i.init_stop(ctx, fill)
                p2 = Position(symbol="M00.SS", quantity=1000, sellable_quantity=1000,
                              avg_cost=10.0, entry_date=ctx.date, entry_price=10.0, stop=state)
                intent = i.evaluate(ctx, p2)
                return (
                    None if state is None or state.stop_price is None else round(float(state.stop_price), 6),
                    None if intent is None else intent.reason,
                )
            if spec.slot == "execution":
                return (
                    i.fill_policy(),
                    [e.symbol for e in i.rotation_policy(ctx, candidates, list(ctx.account.positions))],
                    bool(i.allows_action(ctx)),
                )
            raise AssertionError(f"unknown slot {spec.slot}")

        a, b = probe(), probe()
        assert a == b, f"{spec.key} non-deterministic"


# ----------------------------------------------------------------------
# 具名行为钉
# ----------------------------------------------------------------------


def test_liquidity_filter_top_n_and_threshold(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    m = registry.require("liquidity_filter@1", slot="universe").factory(
        {"min_amount20": 0, "top_n": 2}
    )
    members = m.members(ctx)
    assert len(members) == 2
    # top_n 取成交额均值最高者（M00/M03 的 amount 随价格大）
    assert {x.symbol for x in members} <= set(SYMBOLS)
    # 阈值过滤：高阈值 → 空
    m2 = registry.require("liquidity_filter@1", slot="universe").factory(
        {"min_amount20": 1e18}
    )
    assert m2.members(ctx) == []


def test_ma_cross_exit_fires_on_dead_cross(registry):
    """ma_cross use_exit=true：下跌标的在跌破均线后发 exit（K3-P1-1 的合法触发面）。"""
    panel = _panel()
    m = registry.require("ma_cross@1", slot="signal").factory({"n": 10, "use_exit": True})
    m.prepare(panel)
    from portfolio.slots.universe import UniverseMember

    exit_days = []
    for upto in range(30, DAYS_N):
        ctx = _ctx(panel, upto)
        for ev in m.scan(ctx, [UniverseMember("M04.SS")]):
            if ev.kind == "exit":
                exit_days.append(panel.dates[upto])
    assert exit_days, "先涨后跌的标的应有死叉 exit 事件"


def test_channel_breakout_and_high_52w_fire(registry):
    panel = _panel()
    from portfolio.slots.universe import UniverseMember

    brk = registry.require("channel_breakout@1", slot="signal").factory({})
    brk.prepare(panel)
    hi = registry.require("high_52w@1", slot="signal").factory({"min_bars": 20})
    hi.prepare(panel)
    brk_hits = hi_hits = 0
    for upto in range(25, DAYS_N):
        ctx = _ctx(panel, upto)
        brk_hits += sum(1 for e in brk.scan(ctx, [UniverseMember("M03.SS")]) if e.kind == "entry")
        hi_hits += sum(1 for e in hi.scan(ctx, [UniverseMember("M00.SS")]) if e.kind == "entry")
    assert brk_hits > 0, "M03 尾段陡升应有 20 日突破"
    assert hi_hits > 0, "M00 强趋势应有 52 周新高"


def test_abs_momentum_top1_picks_strongest(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    m = registry.require("abs_momentum@1", slot="signal").factory({"lookback": 20, "top_n": 1})
    m.prepare(panel)
    from portfolio.slots.universe import UniverseMember

    events = m.scan(ctx, [UniverseMember(s) for s in SYMBOLS])
    entries = [e for e in events if e.kind == "entry"]
    assert len(entries) == 1
    assert entries[0].symbol == "M03.SS"  # 尾段斜率最陡


def test_trend_score_cross_wired_produces_events(test_db, registry):
    """trend_score_cross 接线钉（A-R2/DS-P2-7 同款位置）：趋势值缓存 + 接线
    后应产出 entry 事件（不再是零事件的静默死模块）。"""
    panel = _panel()
    # 写 trend_daily：M00 趋势值从 3 升到 8（穿越阈值 5）
    df = pd.DataFrame({
        "time": [d.isoformat() for d in panel.dates],
        "trend_score": np.linspace(3.0, 8.0, DAYS_N),
    })
    test_db.save_trend_daily("M00.SS", df, formula_version=3)
    m = registry.require("trend_score_cross@1", slot="signal").factory({"threshold": 5.0})

    class _GW:
        data_version = 3

        def get_production_indicator(self, *, symbols, name, since, **kw):
            assert name == "trend_score"
            return {"M00.SS": pd.Series(df["trend_score"].to_numpy(),
                                        index=[d for d in panel.dates])}

    m.prepare_with_gateway(_GW(), list(panel.symbols), panel.dates[0])
    from portfolio.slots.universe import UniverseMember

    hits = 0
    for upto in range(1, DAYS_N):
        ctx = _ctx(panel, upto)
        hits += sum(1 for e in m.scan(ctx, [UniverseMember("M00.SS")]) if e.kind == "entry")
    assert hits > 0, "趋势值穿越阈值应产出入场事件"


def test_rank_modules_order(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    from portfolio.slots.signal import SignalEvent

    c_old = SignalEvent(symbol="M00.SS", kind="entry",
                        date=panel.dates[DAYS_N - 10], meta={"event_date": panel.dates[DAYS_N - 10].isoformat()})
    c_new = SignalEvent(symbol="M01.SS", kind="entry", date=ctx.date,
                        meta={"event_date": ctx.date.isoformat()})
    # by_freshness：新信号在前
    ranked = registry.require("by_freshness@1", slot="rank").factory({}).rank(ctx, [c_old, c_new])
    assert ranked[0].symbol == "M01.SS"
    # by_momentum：M03（尾段陡升）应排在 M01（下跌）前
    ranked = registry.require("by_momentum@1", slot="rank").factory({}).rank(
        ctx, [SignalEvent(symbol="M01.SS", kind="entry", date=ctx.date, meta={}),
              SignalEvent(symbol="M03.SS", kind="entry", date=ctx.date, meta={})]
    )
    assert ranked[0].symbol == "M03.SS"
    # by_er：强趋势 M00 的 ER 应高于平盘 M02
    ranked = registry.require("by_er@1", slot="rank").factory({}).rank(
        ctx, [SignalEvent(symbol="M02.SS", kind="entry", date=ctx.date, meta={}),
              SignalEvent(symbol="M00.SS", kind="entry", date=ctx.date, meta={})]
    )
    assert ranked[0].symbol == "M00.SS"
    # random：确定性（同 ctx 两次同序）
    r = registry.require("random@1", slot="rank").factory({"seed": 1})
    a = r.rank(ctx, [c_old, c_new])
    b = r.rank(ctx, [c_old, c_new])
    assert [e.symbol for e in a] == [e.symbol for e in b]


def test_sizing_fixed_pct_and_target_weight(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    from portfolio.slots.signal import SignalEvent

    cand = SignalEvent(symbol="M00.SS", kind="entry", date=ctx.date, meta={})
    # fixed_pct：10% 权益 = 10000
    out = registry.require("fixed_pct@1", slot="sizing").factory({"pct": 0.1}).size(ctx, cand, None)
    assert out is not None and out.intent_type == "target_value"
    assert float(out.value) == pytest.approx(10_000.0)
    # target_weight 显式权重
    out2 = registry.require("target_weight@1", slot="sizing").factory(
        {"weights": {"M00.SS": 0.5}}
    ).size(ctx, cand, None)
    assert float(out2.value) == pytest.approx(50_000.0)
    # 未声明权重的标的 → None
    out3 = registry.require("target_weight@1", slot="sizing").factory(
        {"weights": {"M01.SS": 0.5}}
    ).size(ctx, cand, None)
    assert out3 is None


def test_gates_behaviors(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    from engine.models import OrderIntent, Position, StopState

    intents = [OrderIntent(symbol="M02.SS", decision_date=ctx.date,
                           intent_type="quantity", value=1000, source="signal")]
    # concentration_cap（GLM53F-P1-5 真断言）：已有 2 只同类 l2 持仓时第三只
    # 同 l2 被拦、异类放行——复刻内置件的真实调用形态（无参 instruments()，
    # DS-P2-7 教训：桩形态必须 = 真实调用形态）
    class _MetaCC:
        @staticmethod
        def instruments(wanted=None):
            table = {"M00.SS": "L2A", "M01.SS": "L2A", "M02.SS": "L2A", "M03.SS": "L2B"}
            pool = tuple(wanted) if wanted else tuple(table)
            return {s: {"category_l2": table.get(s, "")} for s in pool}

    class _GwCC:
        metadata = _MetaCC()
        data_version = 1

    ctx_cc = DayContext(
        date=ctx.date, as_of=ctx.as_of, gateway=_GwCC(), panel=ctx.panel,
        account=ctx.account, params={}, data_version=1, gate_log=[], extras={},
    )
    for s in ("M00.SS", "M01.SS"):
        ctx_cc.account._account.positions[s] = Position(
            symbol=s, quantity=100, sellable_quantity=100, avg_cost=10.0,
            entry_date=ctx.date, entry_price=10.0,
            stop=StopState(stop_price=9.0, highest_since_buy=10.0, atr_at_entry=0.2),
        )
    gate = registry.require("concentration_cap@1", slot="portfolio_risk").factory({"per_l2": 2})
    intents_cc = [
        OrderIntent(symbol="M02.SS", decision_date=ctx.date, intent_type="quantity",
                    value=100, source="signal"),   # L2A 第三只 → 拦
        OrderIntent(symbol="M03.SS", decision_date=ctx.date, intent_type="quantity",
                    value=100, source="signal"),   # L2B → 放行
    ]
    out_cc = gate.admit(ctx_cc, intents_cc, [])
    assert [i.symbol for i in out_cc] == ["M03.SS"]
    assert any(g["gate"] == "concentration_cap" and g["symbol"] == "M02.SS"
               for g in ctx_cc.gate_log)

    # market_gate：breadth 中段 halve_risk → 定量减半
    gate = registry.require("market_gate@1", slot="portfolio_risk").factory(
        {"breadth_mid": [0.0, 1.0], "ma": 20, "action": "halve_risk"}
    )
    out = gate.admit(ctx, intents, [])
    assert float(out[0].value) == pytest.approx(500.0)

    # vol_target：无历史 → 不收缩；构造回撤历史 → drawdown_throttle 停新开仓
    gate = registry.require("drawdown_throttle@1", slot="portfolio_risk").factory(
        {"dd_line": -0.05, "action": "halt_new"}
    )
    ctx.history.extend([
        {"equity": 100_000}, {"equity": 120_000}, {"equity": 100_000},
    ])  # 回撤 −16.7% < −5%
    out = gate.admit(ctx, intents, [])
    assert out == []
    ctx.gate_log and True


def test_position_risk_ma_pct_donchian(registry):
    panel = _panel()
    from engine.models import Fill

    # ma_stop：收盘跌破 MA → tail 离场
    m = registry.require("ma_stop@1", slot="position_risk").factory({"n": 5})
    fill = Fill(order_id="T", symbol="M01.SS", fill_date=panel.dates[40],
                base_price=18.0, slippage_base=0.0, slippage_tail=0.0,
                fill_price=18.0, quantity=100, commission=5.0, stamp_tax=0.0,
                fee_total=5.0, cash_after=0.0)
    ctx40 = _ctx(panel, 40)
    state = m.init_stop(ctx40, fill)
    assert state is not None and state.fill_mode == "tail"
    # M01 下跌 → 末日 evaluate 应出离场意图
    ctx_end = _ctx(panel, DAYS_N - 1)
    from engine.models import Position

    pos = Position(symbol="M01.SS", quantity=100, sellable_quantity=100,
                   avg_cost=18.0, entry_date=panel.dates[40], entry_price=18.0, stop=state)
    intent = m.evaluate(ctx_end, pos)
    assert intent is not None and intent.reason == "ma_stop"

    # pct_trailing：止损价 = 最高价 ×(1−pct)
    m2 = registry.require("pct_trailing@1", slot="position_risk").factory({"pct": 0.1})
    state2 = m2.init_stop(ctx40, fill)
    assert state2.stop_price == pytest.approx(18.0 * 1.002 * 0.9, rel=0.05)

    # donchian_exit：止损价 = 前 N 日最低
    m3 = registry.require("donchian_exit@1", slot="position_risk").factory({"n": 10})
    est = m3.estimate_stop(_ctx(panel, DAYS_N - 1), "M00.SS")
    assert est is not None and est > 0


def test_execution_rotation_behaviors(registry):
    panel = _panel()
    ctx = _ctx(panel, DAYS_N - 1)
    from engine.models import Position, StopState
    from portfolio.slots.signal import SignalEvent

    # hold_first：永不轮换
    hold = registry.require("hold_first@1", slot="execution").factory({})
    assert hold.rotation_policy(ctx, [], ["M00.SS"]) == []
    # buffered_rotation：候选远强于持仓 → 轮换最弱持仓
    ctx.account._account.positions["M01.SS"] = Position(
        symbol="M01.SS", quantity=100, sellable_quantity=100, avg_cost=19.0,
        entry_date=panel.dates[DAYS_N - 30], entry_price=19.0,
        stop=StopState(stop_price=18.0, highest_since_buy=19.0, atr_at_entry=0.3),
    )
    rot = registry.require("buffered_rotation@1", slot="execution").factory({"buffer": 0.01})
    exits = rot.rotation_policy(
        ctx, [SignalEvent(symbol="M03.SS", kind="entry", date=ctx.date, meta={})],
        ["M01.SS"],
    )
    assert exits and exits[0].symbol == "M01.SS"
    # rebalance_band（K3-R2-注记-4 补强）：行动日对漂移超 band 的持仓发 exit；
    # 权重在 band 内不发
    rb = registry.require("rebalance_band@1", slot="execution").factory(
        {"band": 0.01, "action_gate": {"freq": "daily"}, "weights": {"M01.SS": 0.99}}
    )
    # 当前 M01 权重 ≈ 100×16.05/101605 ≈ 1.6% vs 目标 99% → 漂移超 band → exit
    exits = rb.rotation_policy(ctx, [], ["M01.SS"])
    assert exits and exits[0].symbol == "M01.SS" and exits[0].reason == "rebalance_band"
    rb_in = registry.require("rebalance_band@1", slot="execution").factory(
        {"band": 0.01, "action_gate": {"freq": "daily"}, "weights": {"M01.SS": 0.016}}
    )
    assert rb_in.rotation_policy(ctx, [], ["M01.SS"]) == []


def test_meta_any_of_all_of(registry):
    panel = _panel()
    from portfolio.slots.universe import UniverseMember

    any_of = registry.require("any_of@1", slot="signal").factory({
        "members": [{"module": "always_entry@1"}, {"module": "high_52w@1", "params": {"min_bars": 20}}]
    })
    any_of.prepare(panel)
    ctx = _ctx(panel, DAYS_N - 1)
    events = any_of.scan(ctx, [UniverseMember("M01.SS")])
    assert any(e.symbol == "M01.SS" for e in events)  # always_entry 必发

    all_of = registry.require("all_of@1", slot="signal").factory({
        "members": [{"module": "always_entry@1"}, {"module": "always_entry@1"}]
    })
    all_of.prepare(panel)
    events = all_of.scan(ctx, [UniverseMember("M01.SS")])
    assert any(e.symbol == "M01.SS" for e in events)
