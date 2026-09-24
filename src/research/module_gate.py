"""新模块自动测试门（详设 §6.2.2，决策 16：不设人审必经门）。

- 数据越界类前视是 L1.5 的职责（as-of 强制，模块物理上取不到未来数据）；
- 逻辑类前视由平台自动探针抓：**前缀稳定性测试**——同模块分别在截断到
  t 与 t+30 的数据上运行，≤t 的输出必须位级一致，不一致 = 有前视，拒绝
  注册（freqtrade lookahead-analysis 同款思路）；
- 契约与确定性自动测试：返回类型 / 无副作用 / 同输入同输出；
- **全插槽三门全跑**（评审 A-P1-5：不允许"非 signal 槽跳过探针"）；
- 人保留抽检与下架权（下架只禁止新引用，不影响已完成实验的记录）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np

from gateway.panel import Panel

_SLOT_PROTOCOLS = {
    "universe": ("members",),
    "signal": ("scan",),
    "rank": ("rank",),
    "sizing": ("size",),
    "portfolio_risk": ("admit",),
    "position_risk": ("init_stop", "evaluate", "estimate_stop"),
    "execution": ("fill_policy", "rotation_policy", "allows_action"),
}


def synthetic_panel(n_days: int = 140, n_symbols: int = 6, seed: int = 5) -> Panel:
    """测试门用的合成面板（固定种子，确定性；恰好 n_days 个交易日）。"""
    rng = np.random.default_rng(seed)
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < n_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    symbols = tuple(f"GATE{i:02d}.SS" for i in range(n_symbols))
    base = np.linspace(10, 20, n_symbols)
    data = {}
    for f in ("open", "high", "low", "close", "volume", "amount"):
        if f in ("volume", "amount"):
            m = np.full((len(days), n_symbols), 2e6 if f == "amount" else 1e6)
        else:
            walk = np.cumsum(rng.normal(0.001, 0.01, (len(days), n_symbols)), axis=0)
            m = base[None, :] * np.exp(walk)
            if f == "high":
                m = m * 1.005
            elif f == "low":
                m = m * 0.995
        data[f] = m
    return Panel(dates=tuple(days), symbols=symbols, data=data,
                 provisional=np.zeros((len(days), n_symbols), dtype=bool))


def _truncate(panel: Panel, upto_idx: int) -> Panel:
    return Panel(
        dates=panel.dates[: upto_idx + 1],
        symbols=panel.symbols,
        data={f: m[: upto_idx + 1, :] for f, m in panel.data.items()},
        provisional=panel.provisional[: upto_idx + 1, :],
    )


def _make_ctx(panel: Panel, upto: int):
    """测试门的 DayContext（账户固定：两只持仓 + 现金）。"""
    from engine.models import Account, Position, StopState
    from portfolio.context import AccountView, DayContext, PanelView

    day = panel.dates[upto]
    account = Account(cash=200_000.0)
    close_map = {}
    for j, symbol in enumerate(panel.symbols):
        v = panel.data["close"][upto, j]
        if np.isfinite(v):
            close_map[symbol] = float(v)
    for symbol in panel.symbols[:2]:
        price = close_map.get(symbol, 10.0)
        account.positions[symbol] = Position(
            symbol=symbol, quantity=1000, sellable_quantity=1000,
            avg_cost=price, entry_date=panel.dates[max(0, upto - 10)],
            entry_price=price,
            stop=StopState(stop_price=price * 0.97, highest_since_buy=price * 1.01,
                           atr_at_entry=price * 0.02),
        )
    return DayContext(
        date=day, as_of=datetime.combine(day, time(15, 0)),
        gateway=_StubBoundGateway(panel.symbols), panel=PanelView(panel, upto),
        account=AccountView(account, close_map),
        params={"_members_count": len(panel.symbols)},
        data_version=1, history=[], gate_log=[], extras={},
    )


def _members(ctx):
    from portfolio.slots.universe import UniverseMember

    return [UniverseMember(symbol=s) for s in ctx.panel.symbols]


def _candidates(ctx):
    from portfolio.slots.signal import SignalEvent

    return [
        SignalEvent(symbol=s, kind="entry", date=ctx.date,
                    meta={"event_date": ctx.date.isoformat()})
        for s in ctx.panel.symbols[2:5]
    ]


def _probe(slot: str, instance, ctx):
    """各插槽的探测动作（返回可比的 JSON 化结果）。"""
    if slot == "universe":
        return [m.symbol for m in instance.members(ctx)]
    if slot == "signal":
        return sorted(
            (ev.symbol, ev.kind, ev.date.isoformat()) for ev in instance.scan(ctx, _members(ctx))
        )
    if slot == "rank":
        return [e.symbol for e in instance.rank(ctx, _candidates(ctx))]
    if slot == "sizing":
        intent = instance.size(ctx, _candidates(ctx)[0], 9.0)
        return None if intent is None else (intent.symbol, round(float(intent.value), 6))
    if slot == "portfolio_risk":
        from engine.models import OrderIntent

        intents = [
            OrderIntent(symbol=s, decision_date=ctx.date, intent_type="quantity",
                        value=1000, source="signal")
            for s in ctx.panel.symbols[2:4]
        ]
        out = instance.admit(ctx, intents, [])
        return [(i.symbol, round(float(i.value), 6)) for i in out]
    if slot == "position_risk":
        from engine.models import Fill, Position

        price = ctx.panel.value(ctx.panel.symbols[0], "close") or 10.0
        fill = Fill(order_id="G-1", symbol=ctx.panel.symbols[0], fill_date=ctx.date,
                    base_price=price, slippage_base=0.0, slippage_tail=0.0,
                    fill_price=price, quantity=1000, commission=5.0, stamp_tax=0.0,
                    fee_total=5.0, cash_after=0.0)
        state = instance.init_stop(ctx, fill)
        pos = Position(symbol=ctx.panel.symbols[0], quantity=1000, sellable_quantity=1000,
                       avg_cost=price, entry_date=ctx.date, entry_price=price, stop=state)
        intent = instance.evaluate(ctx, pos)
        return (
            None if state.stop_price is None else round(float(state.stop_price), 6),
            None if intent is None else (intent.reason, intent.fill_mode),
        )
    if slot == "execution":
        policy = instance.fill_policy()
        exits = instance.rotation_policy(ctx, _candidates(ctx), list(ctx.account.positions))
        return (
            {k: round(float(v), 6) for k, v in policy.items()},
            [e.symbol for e in exits],
            bool(instance.allows_action(ctx)),
        )
    raise ValueError(f"unknown slot: {slot}")


class _StubBoundGateway:
    """测试门的元数据桩（评审 DS-P2-7 + DS-R2：无参调用形态必须复刻内置件）。

    内置模块（category_filter/liquidity_filter/concentration_cap）以
    `instruments()` **无参**调用取全量元数据——桩必须同样支持无参（返回
    测试面板全部标的的行），否则"与内置同形"的合法模块仍被误杀。
    """

    data_version = 1

    def __init__(self, panel_symbols=()) -> None:
        self._symbols = tuple(panel_symbols)

    @property
    def metadata(self):
        symbols = self._symbols

        class _Meta:
            @staticmethod
            def instruments(wanted=None):
                pool = tuple(wanted) if wanted else symbols
                return {
                    s: {"category_l1": "T", "category_l2": "T2", "category_l3": "",
                        "asset_type": "etf", "enabled": 1, "start_date": "2020-01-01"}
                    for s in pool
                }

        return _Meta()

    def get_production_indicator(self, *, symbols, name, since, **kwargs):
        return {}


def _instantiate(factory, params: dict, panel):
    instance = factory(params)
    if hasattr(instance, "prepare"):
        instance.prepare(panel)
    if hasattr(instance, "prepare_with_gateway"):
        instance.prepare_with_gateway(_StubBoundGateway(panel.symbols), list(panel.symbols), panel.dates[0])
    return instance


def run_module_gate(factory, *, slot: str, params: dict | None = None) -> dict:
    """契约 + 确定性 + 前缀稳定性三门（全插槽）。返回 {passed, checks}。"""
    checks: dict[str, dict] = {}
    params = params or {}

    # 1. 契约：可实例化 + 协议方法存在
    protocol = _SLOT_PROTOCOLS.get(slot)
    if protocol is None:
        return {"passed": False, "checks": {"contract": {"ok": False, "error": f"unknown slot {slot}"}}}
    try:
        instance = factory(params)
        missing = [m for m in protocol if not callable(getattr(instance, m, None))]
        checks["contract"] = {"ok": not missing, "missing": missing}
        if missing:
            return {"passed": False, "checks": checks}
    except Exception as exc:
        checks["contract"] = {"ok": False, "error": str(exc)[:300]}
        return {"passed": False, "checks": checks}

    # 2. 确定性：同输入同输出（同一面板两次探测）
    try:
        panel = synthetic_panel()
        upto = len(panel.dates) - 1
        a1 = _probe(slot, _instantiate(factory, params, panel), _make_ctx(panel, upto))
        a2 = _probe(slot, _instantiate(factory, params, panel), _make_ctx(panel, upto))
        checks["determinism"] = {"ok": a1 == a2}
        if a1 != a2:
            return {"passed": False, "checks": checks}
    except Exception as exc:
        checks["determinism"] = {"ok": False, "error": str(exc)[:300]}
        return {"passed": False, "checks": checks}

    # 3. 前缀稳定性：截断到 cut 与全量分别 prepare，≤cut 的探测输出位级一致
    try:
        full = synthetic_panel(n_days=140)
        cut = 70
        cut_panel = _truncate(full, cut)
        out_full = _probe(slot, _instantiate(factory, params, full), _make_ctx(full, cut))
        out_cut = _probe(
            slot, _instantiate(factory, params, cut_panel),
            _make_ctx(cut_panel, len(cut_panel.dates) - 1),
        )
        checks["prefix_stability"] = {"ok": out_full == out_cut}
        if out_full != out_cut:
            return {"passed": False, "checks": checks}
    except Exception as exc:
        checks["prefix_stability"] = {"ok": False, "error": str(exc)[:300]}
        return {"passed": False, "checks": checks}

    return {"passed": True, "checks": checks}
