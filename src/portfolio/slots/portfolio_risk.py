"""`portfolio_risk` 组合风控插槽（详设 §5.2.5）。

protocol PortfolioRiskModule:
    admit(ctx, intents, pending_exits) -> list[OrderIntent]

对"排序 + 定量后的买入意图清单"做总风险卡控，返回放行清单（可截断、
可调量，不调序——调序是 rank 的事）。每个 gate 拒绝时记原因落 run 日志
（"被风控拦下的候选"与未成交订单一样是研究素材）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.models import OrderIntent
from portfolio.registry import REGISTRY, ModuleSpec


def _log(ctx, gate: str, symbol: str, reason: str) -> None:
    ctx.gate_log.append({
        "date": ctx.date.isoformat(), "gate": gate, "symbol": symbol, "reason": reason,
    })


def _intent_qty_estimate(ctx, intent: OrderIntent) -> float:
    price = ctx.panel.value(intent.symbol, "close") or 0.0
    if intent.intent_type == "quantity":
        return float(intent.value)
    return float(intent.value) / price if price > 0 else 0.0


class SlotLimitGate:
    """持仓数 ≤ N（考虑 pending_exits 释放的槽位）。"""

    def __init__(self, params: dict) -> None:
        self.max_positions = int(params.get("max_positions", 10))

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        held = len(ctx.account.positions)
        freeing = sum(1 for s in pending_exits if s in ctx.account.positions)
        available = max(self.max_positions - (held - freeing), 0)
        out: list[OrderIntent] = []
        for intent in intents:
            if intent.symbol in ctx.account.positions:
                # 对已持仓标的的 entry 静默丢弃也要留痕（回测器
                # 上游虽已过滤，gate 作为最后防线不该有"无声分支"）
                _log(ctx, "slot_limit", intent.symbol, "already_held (entry ignored)")
                continue
            if len(out) < available:
                out.append(intent)
            else:
                _log(ctx, "slot_limit", intent.symbol, f"positions>{self.max_positions}")
        return out


class HeatCapGate:
    """加入后组合 heat ≤ equity × 上限（heat 由引擎账户提供）。"""

    def __init__(self, params: dict) -> None:
        self.max_heat_pct = float(params.get("max_heat_pct", 0.06))

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        equity = ctx.account.equity()
        if equity <= 0:
            return []
        heat = ctx.account.heat()
        if heat is None:
            # 组合热不可知（存在 time_stop/breakeven 等按设计无止损价的持仓）：
            # heat_cap 无从精确卡控——**告警放行，不冻结**（DS-R2 P2：静默拒绝全部
            # 新开仓会让"止损选型"课题得到一批原因隐蔽的零成交实验）。
            # 明确的"不卡控"状态写在 gate_log 与 run warnings 里，供 verdict 聚合。
            # unstopped 持仓清单落 gate_log（旧实现该变量赋值后未用，
            # 日志里只有占位 "*"——无法定位是哪些持仓导致 heat 不可知）。
            unstopped_syms = ctx.account.unstopped_symbols() \
                if hasattr(ctx.account, "unstopped_symbols") else []
            for s in unstopped_syms:
                _log(ctx, "heat_cap", s, "no_stop_price (heat unknown — cap NOT enforced)")
            if not unstopped_syms:
                _log(ctx, "heat_cap", "*", "heat unknown (unstopped positions) — cap NOT enforced")
            return intents
        heat = heat or 0.0
        cap = equity * self.max_heat_pct
        est_stops: dict = ctx.params.get("_est_stops", {})
        # 候选自身没有止损估计（持仓风控模块按设计不给，如 time_stop/
        # breakeven/none）：与"组合热不可知"同一裁决口径——**告警放行**，
        # cap 对这类候选不生效，并在 gate_log 与 run warnings 里明说。
        # 旧实现此处 `continue`（拒绝全部候选），
        # 与 backtester 同步发出的"heat_cap 本 run 不卡控（告警放行）"完全
        # 相反 → 该策略族静默零成交（实证 breakeven/time_stop/none 全 0 笔，
        # hard_stop 对照组 9 笔）。"止损选型"恰是首个课题。
        out: list[OrderIntent] = []
        for intent in intents:
            stop = est_stops.get(intent.symbol)
            price = ctx.panel.value(intent.symbol, "close")
            if stop is None or price is None:
                # 原因必须如实（旧写法把 price 缺失也写成
                # no_stop_estimate，审计留痕说谎）
                reason = (
                    "no_stop_estimate" if stop is None and price is not None
                    else "no_price_estimate" if price is None and stop is not None
                    else "no_stop_estimate+no_price"
                )
                _log(ctx, "heat_cap", intent.symbol,
                     f"{reason} — cap NOT enforced for this candidate")
                out.append(intent)
                continue
            qty = _intent_qty_estimate(ctx, intent)
            add = max(0.0, (price - stop) * qty)
            if heat + add <= cap:
                heat += add
                out.append(intent)
            else:
                _log(ctx, "heat_cap", intent.symbol,
                     f"heat {heat + add:.0f} > cap {cap:.0f}")
        return out


class ConcentrationCapGate:
    """同 category_l2 持仓 ≤ M（成员类目来自 universe）。"""

    def __init__(self, params: dict) -> None:
        self.per_l2 = int(params.get("per_l2", 2))
        self._categories: dict[str, str] = {}

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        if not self._categories:
            meta = ctx.gateway.metadata.instruments()
            self._categories = {
                s: str(m.get("category_l2") or "") for s, m in meta.items()
            }
        counts: dict[str, int] = {}
        for symbol in ctx.account.positions:
            cat = self._categories.get(symbol, "")
            counts[cat] = counts.get(cat, 0) + 1
        out: list[OrderIntent] = []
        for intent in intents:
            cat = self._categories.get(intent.symbol, "")
            if counts.get(cat, 0) >= self.per_l2:
                _log(ctx, "concentration_cap", intent.symbol,
                     f"category_l2 {cat!r} >= {self.per_l2}")
                continue
            counts[cat] = counts.get(cat, 0) + 1
            out.append(intent)
        return out


class MarketGate:
    """大盘闸门（ex-ante）。两种口径：

    - breadth 模式（params.breadth_mid=[lo,hi]）：universe 内站上 SMA200 的
      比例处于中段时，按 action 减半风险预算或暂停新开仓；
    - benchmark 模式（params.benchmark + params.ma）：基准收盘跌破 SMA(ma)
      时按 action 暂停/减半（详设 §5.10.2 实验二形态）。
    action: "halt"（暂停新开仓）| "halve_risk"（定量减半）。
    """

    def __init__(self, params: dict) -> None:
        self.breadth_mid = params.get("breadth_mid")  # [lo, hi]
        self.benchmark = params.get("benchmark")
        self.ma = int(params.get("ma", 200))
        self.action = str(params.get("action", "halt"))
        self._sma_cache: dict[str, pd.Series] = {}

    def _breadth(self, ctx) -> float | None:
        close = ctx.panel.matrix("close")
        if close.shape[0] < self.ma:
            return None
        window = close[-self.ma:, :]
        with np.errstate(all="ignore"):
            sma = np.nanmean(window, axis=0)
        cur = close[-1, :]
        valid = np.isfinite(sma) & np.isfinite(cur)
        if not valid.any():
            return None
        return float(np.mean(cur[valid] > sma[valid]))

    def _benchmark_below_ma(self, ctx) -> bool | None:
        if not self.benchmark:
            return None
        bm = str(self.benchmark).upper()
        if bm not in ctx.panel.symbols:
            # GLM53F-P2-20：基准不在面板（universe 不含指数）时闸门全程空转——
            # 留痕可见，不能静默假阴性（"闸门有没有用"的实验需要能分辨）
            _log(ctx, "market_gate", bm, "benchmark 不在面板，闸门空转（检查 universe/数据）")
            return None
        series = ctx.panel.series(bm, "close")
        series = series[np.isfinite(series)]
        if len(series) < self.ma:
            return None
        ma = float(series[-self.ma:].mean())
        return bool(series[-1] < ma)

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        triggered = False
        if self.benchmark:
            below = self._benchmark_below_ma(ctx)
            triggered = bool(below)
        elif self.breadth_mid:
            breadth = self._breadth(ctx)
            if breadth is not None:
                lo, hi = float(self.breadth_mid[0]), float(self.breadth_mid[1])
                triggered = lo <= breadth <= hi
        if not triggered:
            return intents
        if self.action == "halt":
            for intent in intents:
                _log(ctx, "market_gate", intent.symbol, "gate=halt")
            return []
        # halve_risk：定量减半（不动顺序）
        out = []
        for intent in intents:
            out.append(OrderIntent(
                symbol=intent.symbol, decision_date=intent.decision_date,
                intent_type=intent.intent_type, value=float(intent.value) / 2.0,
                source=intent.source,
            ))
        return out


class VolTargetGate:
    """组合级波动率目标（Carver 式）：实际波动率超目标时按比例收缩新开仓。

    params: target_vol=0.15（年化）、lookback=20（日收益窗口）。
    组合实际波动率用本运行净值序列（ctx.history）计算。
    """

    def __init__(self, params: dict) -> None:
        self.target_vol = float(params.get("target_vol", 0.15))
        self.lookback = int(params.get("lookback", 20))

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        equities = ctx.history_equity()
        if len(equities) < self.lookback + 1:
            return intents
        rets = np.diff(equities[-(self.lookback + 1):]) / equities[-(self.lookback + 1):-1]
        realized = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0.0
        if realized <= self.target_vol or realized <= 0:
            return intents
        scale = self.target_vol / realized
        out = []
        for intent in intents:
            _log(ctx, "vol_target", intent.symbol,
                 f"vol {realized:.2f} > {self.target_vol:.2f}, scale {scale:.2f}")
            out.append(OrderIntent(
                symbol=intent.symbol, decision_date=intent.decision_date,
                intent_type=intent.intent_type, value=float(intent.value) * scale,
                source=intent.source,
            ))
        return out


class DrawdownThrottleGate:
    """回撤节流：组合回撤越线时降风险预算或暂停新开仓，修复后恢复。

    params: dd_line=-0.10、action="halt_new"|"halve_risk"。
    """

    def __init__(self, params: dict) -> None:
        self.dd_line = float(params.get("dd_line", -0.10))
        self.action = str(params.get("action", "halt_new"))

    def admit(self, ctx, intents: list[OrderIntent], pending_exits: list[str]) -> list[OrderIntent]:
        equities = ctx.history_equity()
        if len(equities) < 2:
            return intents
        peak = float(np.max(equities))
        dd = equities[-1] / peak - 1.0 if peak > 0 else 0.0
        if dd >= self.dd_line:
            return intents
        if self.action == "halt_new":
            for intent in intents:
                _log(ctx, "drawdown_throttle", intent.symbol, f"dd {dd:.2%} < {self.dd_line:.2%}")
            return []
        return [
            OrderIntent(symbol=i.symbol, decision_date=i.decision_date,
                        intent_type=i.intent_type, value=float(i.value) / 2.0,
                        source=i.source)
            for i in intents
        ]


def register_portfolio_risk_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="slot_limit", version=1, factory=SlotLimitGate,
        params_schema={"max_positions": {"type": "integer", "default": 10, "min": 1, "max": 100}},
        description="持仓数上限",
    ))
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="heat_cap", version=1, factory=HeatCapGate,
        params_schema={"max_heat_pct": {"type": "number", "default": 0.06, "min": 0.005, "max": 0.5}},
        description="组合热上限",
    ))
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="concentration_cap", version=1, factory=ConcentrationCapGate,
        params_schema={"per_l2": {"type": "integer", "default": 2, "min": 1, "max": 50}},
        description="同二级类目持仓上限",
    ))
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="market_gate", version=1, factory=MarketGate,
        params_schema={
            "breadth_mid": {"type": "list"},
            "benchmark": {"type": "string"},
            "ma": {"type": "integer", "default": 200, "min": 20, "max": 400},
            "action": {"type": "string", "default": "halt", "choices": ["halt", "halve_risk"]},
        },
        description="大盘闸门（breadth 中段 / 基准跌破均线）",
    ))
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="vol_target", version=1, factory=VolTargetGate,
        params_schema={
            "target_vol": {"type": "number", "default": 0.15, "min": 0.01, "max": 1.0},
            "lookback": {"type": "integer", "default": 20, "min": 10, "max": 120},
        },
        description="组合波动率目标收缩",
    ))
    registry.register(ModuleSpec(
        slot="portfolio_risk", name="drawdown_throttle", version=1, factory=DrawdownThrottleGate,
        params_schema={
            "dd_line": {"type": "number", "default": -0.10, "min": -0.5, "max": -0.01},
            "action": {"type": "string", "default": "halt_new", "choices": ["halt_new", "halve_risk"]},
        },
        description="回撤节流",
    ))
