"""`execution` 执行插槽（详设 §5.2.7）+ 元模块 any_of/all_of（§5.2.8）。

protocol ExecutionModule:
    fill_policy() -> dict            # 尾盘模型参数（滑点等）
    rotation_policy(ctx, candidates, holdings) -> list[ExitOrderIntent]
    allows_action(ctx) -> bool       # 行动门控（日历驱动策略的频率参数化）

MVP 的默认执行 = 事件驱动、每日可行动（hold_first）；日历驱动（月度检查/
定期再平衡）是 execution 的频率参数化变体，不是第二种范式（详设 §5.11
缺口①的解法）。风控评估不受门控影响（每日照常）。
"""

from __future__ import annotations

import numpy as np

from engine.models import ExitOrderIntent, Position
from portfolio.registry import REGISTRY, ModuleRegistry, ModuleSpec, validate_params


def _is_first_day_of_period(ctx, freq: str) -> bool:
    """行动门控：monthly=每月首个交易日；weekly=每周首个交易日；
    daily=每日；every_n:N=每 N 个**日历日序数取模**（≈每 0.7N 个交易日）。

    every_n 口径说明（DS-P3-9 取舍 + GLM53F-P2-22 文档对齐）：用日历日序数
    取模是为了行动日不随回测窗口起点漂移（同配置换窗口仍是同一个实验）；
    代价是节奏比"每 N 个交易日"密约 30% 且遇长假漂移。一期取前者，
    交易日序数口径（固定纪元的全市场交易日计数）留待数据线二期。
    """
    idx = ctx.panel.upto
    if freq == "daily":
        return True
    if freq.startswith("every_n:"):
        # 日历日序数取模（见 docstring 口径说明）
        n = max(int(freq.split(":")[1]), 1)
        return ctx.date.toordinal() % n == 0
    dates = ctx.panel.dates
    if idx == 0:
        return True
    prev, cur = dates[idx - 1], dates[idx]
    if freq == "monthly":
        return (prev.year, prev.month) != (cur.year, cur.month)
    if freq == "yearly":
        return prev.year != cur.year
    if freq == "weekly":
        return prev.isocalendar()[:2] != cur.isocalendar()[:2]
    return True


class TailSessionExecution:
    """T 日尾盘成交（基准价=收盘，滑点参数化）+ 可选行动门控。

    存在理由：让"成交时点"成为可实验的插槽，而不是写死在引擎里。
    params: slippage_base=0.002, slippage_tail=0.001,
            action_gate={freq: daily|weekly|monthly|yearly|every_n:N}
    """

    def __init__(self, params: dict) -> None:
        self.slippage_base = float(params.get("slippage_base", 0.002))
        self.slippage_tail = float(params.get("slippage_tail", 0.001))
        gate = params.get("action_gate") or {}
        self.freq = str(gate.get("freq", "daily"))

    def fill_policy(self) -> dict:
        return {"slippage_base": self.slippage_base, "slippage_tail": self.slippage_tail}

    def allows_action(self, ctx) -> bool:
        return _is_first_day_of_period(ctx, self.freq)

    def rotation_policy(self, ctx, candidates, holdings) -> list[ExitOrderIntent]:
        return []  # 持有优先，不主动踢仓


class HoldFirstExecution(TailSessionExecution):
    """显式持有优先（与 tail_session 同 fill 语义，轮换恒空）。"""


class BufferedRotationExecution(TailSessionExecution):
    """缓冲轮换（qlib TopkDropout 同族）：候选分数 > 最差持仓 × (1+buffer)
    才换仓。params: buffer=0.05, lookback=20（动量分）, max_swaps=1。"""

    def __init__(self, params: dict) -> None:
        super().__init__(params)
        self.buffer = float(params.get("buffer", 0.05))
        self.lookback = int(params.get("lookback", 20))
        self.max_swaps = int(params.get("max_swaps", 1))

    def _momentum(self, ctx, symbol: str) -> float:
        series = ctx.panel.series(symbol, "close")
        series = series[np.isfinite(series)]
        if len(series) <= self.lookback:
            return -np.inf
        base = series[-self.lookback - 1]
        return float(series[-1] / base - 1.0) if base > 0 else -np.inf

    def rotation_policy(self, ctx, candidates, holdings) -> list[ExitOrderIntent]:
        if not candidates or not holdings:
            return []
        # max_swaps：单行动日至多换 N 只（loop-review R1-P3-2：参数此前
        # 声明未用，恒至多换 1 只——现按声明的参数域真实生效）
        ranked_holdings = sorted(holdings, key=lambda s: self._momentum(ctx, s))
        ranked_candidates = sorted(candidates, key=lambda e: self._momentum(ctx, e.symbol), reverse=True)
        exits: list[ExitOrderIntent] = []
        swaps = 0
        for worst, best in zip(ranked_holdings, ranked_candidates):
            if swaps >= self.max_swaps:
                break
            worst_score = self._momentum(ctx, worst)
            best_score = self._momentum(ctx, best.symbol)
            if worst_score == -np.inf or best_score == -np.inf:
                break
            threshold = worst_score * (1.0 + self.buffer) if worst_score >= 0 else 0.0
            if best_score > threshold:
                exits.append(ExitOrderIntent(
                    symbol=worst, decision_date=ctx.date, fill_mode="tail",
                    source="rotation", reason="buffered_rotation",
                ))
                swaps += 1
            else:
                break  # 最好候选都过不了最差持仓的阈值，其余更不可能
        return exits


class RebalanceBandExecution(TailSessionExecution):
    """带宽/日历再平衡（MVP 整仓语义内）：行动日仅对 **overweight**（实际
    权重高于目标超 band）的持仓发 exit。params: band=0.05,
    action_gate={freq}, weights={symbol: w}（缺省 = 成员均分 1/N）。

    口径（loop-review R1-P1-1）：underweight **不动作**——MVP 无加仓/
    部分卖出（§5.4.2），且"同标的同日边卖边买"被禁止（当日卖出的标的
    当日不能回补），跌了卖出只会把"跌了买回"的再平衡变成割底空仓。
    underweight 的修复语义（同日卖超配买低配 / 部分卖出）是设计级变更，
    走架构修订，不在此静默近似。"""

    def __init__(self, params: dict) -> None:
        super().__init__(params)
        self.band = float(params.get("band", 0.05))
        self.weights = {str(k).strip().upper(): float(v)
                        for k, v in (params.get("weights") or {}).items()}

    def rotation_policy(self, ctx, candidates, holdings) -> list[ExitOrderIntent]:
        if not holdings or not self.allows_action(ctx):
            return []
        equity = ctx.account.equity()
        if equity <= 0:
            return []
        n_members = int(ctx.params.get("_members_count") or 0)
        exits: list[ExitOrderIntent] = []
        for symbol in holdings:
            if self.weights:
                target = self.weights.get(symbol)
            else:
                target = (1.0 / n_members) if n_members > 0 else None
            if target is None:
                continue
            price = ctx.panel.value(symbol, "close")
            if price is None:
                continue
            actual = ctx.account.positions[symbol].quantity * price / equity
            if actual - target > self.band:  # 仅 overweight；underweight 不动作
                exits.append(ExitOrderIntent(
                    symbol=symbol, decision_date=ctx.date, fill_mode="tail",
                    source="rebalance", reason="rebalance_band",
                ))
        return exits


# ----------------------------------------------------------------------
# 元模块（详设 §5.2.8）：any_of / all_of——组合逻辑本身做成元模块，
# 全插槽通用；嵌套有界（元模块内不再嵌元模块）。
# ----------------------------------------------------------------------


class _MetaBase:
    def __init__(self, params: dict, registry: ModuleRegistry, slot: str) -> None:
        self.registry = registry
        self.slot = slot
        members = params.get("members") or []
        if not members:
            raise ValueError(f"meta module {slot} requires non-empty members")
        self._subs: list[tuple[object, str]] = []
        for item in members:
            ref = item.get("module") if isinstance(item, dict) else item
            sub_params = (item.get("params") if isinstance(item, dict) else None) or {}
            name = str(ref or "")
            if name.startswith(("any_of", "all_of")):
                raise ValueError("meta modules cannot nest meta modules")
            spec = registry.require(name, slot=slot)
            # GLM53F-P2-19：成员参数同样过 schema 校验（"载入即拒绝"对元模块
            # 成员不豁免——atr_mul:-5 包进 members 不许静默通过）
            normalized, p_errors = validate_params(spec.params_schema, sub_params)
            if p_errors:
                raise ValueError(
                    f"meta member {name} params invalid: {'; '.join(p_errors)}"
                )
            sub_instance = spec.factory(normalized)
            # loop-review R1-P2-2（V2 验收修正）：成员实例必须带注册键——
            # 顶层实例由 instantiate_modules 打 _registered_key，元模块成员
            # 此前没人打，live 止损重建按键分派时全落兜底分支（组合止损恒
            # None）。与顶层同口径在此补上。
            sub_instance._registered_key = spec.key
            self._subs.append((sub_instance, spec.key))

    def prepare_with_gateway(self, bound_gateway, symbols, start) -> None:
        """GLM53F-P1-2：生产指标接线逐子转发——backtester/live/evaluations
        的 hasattr 探测都在顶层实例上做，元模块不实现 = 组合腿静默零信号。"""
        for sub, _ in self._subs:
            if hasattr(sub, "prepare_with_gateway"):
                sub.prepare_with_gateway(bound_gateway, symbols, start)


class AnyOfSignal(_MetaBase):
    """任一子模块触发即生效（entry 并集去重，exit 并集）。"""

    def __init__(self, params: dict, registry: ModuleRegistry) -> None:
        super().__init__(params, registry, "signal")

    def prepare(self, panel) -> None:
        for sub, _ in self._subs:
            if hasattr(sub, "prepare"):
                sub.prepare(panel)

    def scan(self, ctx, members):
        from portfolio.slots.signal import SignalEvent

        entries: dict[str, SignalEvent] = {}
        exits: dict[str, SignalEvent] = {}
        for sub, _ in self._subs:
            for ev in sub.scan(ctx, members):
                bucket = entries if ev.kind == "entry" else exits
                prev = bucket.get(ev.symbol)
                if prev is None or ev.date < prev.date:
                    bucket[ev.symbol] = ev
        return [*entries.values(), *exits.values()]


class AllOfSignal(_MetaBase):
    """全部子模块同时成立才生效（入场条件叠加；exit 任一成立即退）。"""

    def __init__(self, params: dict, registry: ModuleRegistry) -> None:
        super().__init__(params, registry, "signal")

    def prepare(self, panel) -> None:
        for sub, _ in self._subs:
            if hasattr(sub, "prepare"):
                sub.prepare(panel)

    def scan(self, ctx, members):
        from portfolio.slots.signal import SignalEvent

        per_sub = [sub.scan(ctx, members) for sub, _ in self._subs]
        if not per_sub:
            return []
        entry_sets = [{e.symbol: e for e in events if e.kind == "entry"} for events in per_sub]
        exit_sets = [{e.symbol: e for e in events if e.kind == "exit"} for events in per_sub]
        common = set.intersection(*[set(s) for s in entry_sets]) if entry_sets else set()
        out = []
        for symbol in sorted(common):
            latest = max(entry_sets[i][symbol].date for i in range(len(entry_sets)))
            out.append(SignalEvent(symbol=symbol, kind="entry", date=latest,
                                   meta={"event_date": latest.isoformat(), "all_of": True}))
        any_exit = set()
        for s in exit_sets:
            any_exit |= set(s)
        for symbol in sorted(any_exit):
            out.append(SignalEvent(symbol=symbol, kind="exit", date=ctx.date, meta={}))
        return out


class AnyOfPositionRisk(_MetaBase):
    """止损组合的默认语义：谁先到谁触发（子状态存 module_state["subs"]）。"""

    def __init__(self, params: dict, registry: ModuleRegistry) -> None:
        super().__init__(params, registry, "position_risk")

    def estimate_stop(self, ctx, symbol: str):
        stops = [m.estimate_stop(ctx, symbol) for m, _ in self._subs]
        stops = [s for s in stops if s is not None]
        return max(stops) if stops else None  # 最紧（最高）止损先触发

    def init_stop(self, ctx, fill):
        from engine.models import StopState

        sub_states = []
        refs = []
        for module, ref in self._subs:
            sub_states.append(module.init_stop(ctx, fill))
            refs.append(ref)
        # 公共 stop_price = 最紧有效止损（heat 与快照用）
        prices = [s.stop_price for s in sub_states if s.stop_price is not None]
        return StopState(
            stop_price=max(prices) if prices else None,
            highest_since_buy=max(s.highest_since_buy for s in sub_states),
            atr_at_entry=max(s.atr_at_entry for s in sub_states),
            fill_mode="intraday_stop",
            module_state={"any_of": refs, "subs": [self._state_to_dict(s) for s in sub_states]},
        )

    def evaluate(self, ctx, position):
        state = position.stop
        if state is None:
            return None
        subs_raw = state.module_state.get("subs", [])
        intents = []
        new_sub_states = []
        for (module, ref), sub_raw in zip(self._subs, subs_raw):
            sub_state = self._state_from_dict(sub_raw)
            shadow = Position(
                symbol=position.symbol, quantity=position.quantity,
                sellable_quantity=position.sellable_quantity, avg_cost=position.avg_cost,
                entry_date=position.entry_date, entry_price=position.entry_price,
                stop=sub_state,
            )
            intent = module.evaluate(ctx, shadow)
            new_sub_states.append(self._state_to_dict(shadow.stop))
            if intent is not None:
                intents.append(intent)
        state.module_state["subs"] = new_sub_states
        prices = [s.get("stop_price") for s in new_sub_states if s.get("stop_price") is not None]
        state.stop_price = max(prices) if prices else None
        state.highest_since_buy = max(s.get("highest_since_buy", 0.0) for s in new_sub_states)
        if not intents:
            return None
        # 谁先到谁触发：盘中价触发者优先于收盘确认型；同为盘中取价高者
        intraday = [i for i in intents if i.fill_mode == "intraday_stop"]
        if intraday:
            return max(intraday, key=lambda i: i.stop_price or 0)
        return intents[0]

    @staticmethod
    def _state_to_dict(state) -> dict:
        return {
            "stop_price": state.stop_price,
            "highest_since_buy": state.highest_since_buy,
            "atr_at_entry": state.atr_at_entry,
            "fill_mode": state.fill_mode,
            "heat_approximate": state.heat_approximate,
            "module_state": dict(state.module_state),
        }

    @staticmethod
    def _state_from_dict(data: dict):
        from engine.models import StopState

        return StopState(
            stop_price=data.get("stop_price"),
            highest_since_buy=float(data.get("highest_since_buy", 0.0)),
            atr_at_entry=float(data.get("atr_at_entry", 0.0)),
            fill_mode=data.get("fill_mode", "intraday_stop"),
            heat_approximate=bool(data.get("heat_approximate", False)),
            module_state=dict(data.get("module_state", {})),
        )


def register_execution_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="execution", name="tail_session", version=1, factory=TailSessionExecution,
        params_schema={
            "slippage_base": {"type": "number", "default": 0.002, "min": 0, "max": 0.05},
            "slippage_tail": {"type": "number", "default": 0.001, "min": 0, "max": 0.05},
            "action_gate": {"type": "dict"},
        },
        description="T 日尾盘成交（含行动门控 freq 参数）",
    ))
    registry.register(ModuleSpec(
        slot="execution", name="hold_first", version=1, factory=HoldFirstExecution,
        params_schema={
            "slippage_base": {"type": "number", "default": 0.002, "min": 0, "max": 0.05},
            "slippage_tail": {"type": "number", "default": 0.001, "min": 0, "max": 0.05},
            "action_gate": {"type": "dict"},
        },
        description="尾盘成交 + 持有优先不主动踢仓",
    ))
    registry.register(ModuleSpec(
        slot="execution", name="buffered_rotation", version=1, factory=BufferedRotationExecution,
        params_schema={
            "slippage_base": {"type": "number", "default": 0.002, "min": 0, "max": 0.05},
            "slippage_tail": {"type": "number", "default": 0.001, "min": 0, "max": 0.05},
            "action_gate": {"type": "dict"},
            "buffer": {"type": "number", "default": 0.05, "min": 0.0, "max": 1.0},
            "lookback": {"type": "integer", "default": 20, "min": 5, "max": 300},
            "max_swaps": {"type": "integer", "default": 1, "min": 1, "max": 10},
        },
        description="缓冲带宽轮换（候选分 > 最差持仓 ×(1+buffer) 才换）",
    ))
    registry.register(ModuleSpec(
        slot="execution", name="rebalance_band", version=1, factory=RebalanceBandExecution,
        params_schema={
            "slippage_base": {"type": "number", "default": 0.002, "min": 0, "max": 0.05},
            "slippage_tail": {"type": "number", "default": 0.001, "min": 0, "max": 0.05},
            "action_gate": {"type": "dict"},
            "band": {"type": "number", "default": 0.05, "min": 0.005, "max": 0.5},
            "weights": {"type": "dict"},
        },
        description="带宽/日历再平衡（MVP 整仓语义内的先卖后买）",
    ))


def register_meta_modules(registry=REGISTRY) -> None:
    """any_of / all_of（成员清单是参数的一部分——增删成员 = 单槽 diff）。"""
    registry.register(ModuleSpec(
        slot="signal", name="any_of", version=1,
        factory=lambda p: AnyOfSignal(p, registry),
        params_schema={"members": {"type": "list", "required": True}},
        kind="builtin", description="信号或组合（任一触发）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="all_of", version=1,
        factory=lambda p: AllOfSignal(p, registry),
        params_schema={"members": {"type": "list", "required": True}},
        kind="builtin", description="信号与组合（全部成立）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="any_of", version=1,
        factory=lambda p: AnyOfPositionRisk(p, registry),
        params_schema={"members": {"type": "list", "required": True}},
        kind="builtin", description="止损组合默认语义（谁先到谁触发）",
    ))
