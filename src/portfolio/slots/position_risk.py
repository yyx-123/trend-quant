"""`position_risk` 持仓风控插槽（详设 §5.2.6）：止损公式可插拔。

protocol PositionRiskModule:
    init_stop(ctx, fill) -> StopState        # 买入成交后初始化（ATR 含当根）
    evaluate(ctx, position) -> ExitOrderIntent | None  # 每日评估（T-1 状态判定）
    estimate_stop(ctx, symbol) -> float | None          # sizing 的预估输入

公式实现委托 engine/stops.py（收编后的唯一实现：实际买入价基准、ATR 含
当根；日内路径口径——T 日判定用 T-1 状态）。首批九个：hard_stop /
chandelier / ratchet / ma_stop / pct_trailing / donchian_exit / breakeven /
time_stop / none（§4.3.1 全族）。
"""

from __future__ import annotations

import numpy as np

from engine import stops as eng_stops
from engine.models import ExitOrderIntent, Position, StopState
from portfolio.registry import REGISTRY, ModuleSpec


def _today_bar(ctx, symbol: str) -> dict | None:
    return ctx.panel.bar(symbol)


def _atr(ctx, symbol: str, day_offset: int = 0, period: int = 20) -> float:
    """ATR 面板读数：day_offset=0 含当根（入场初始化用），=-1 为 T-1（逐日判定用）。"""
    matrices = ctx.extras.get("atr", {})
    matrix = matrices.get(period)
    if matrix is None:
        return 0.0
    col = ctx.panel.symbol_col(symbol)
    if col is None:
        return 0.0
    row = ctx.panel.upto + day_offset
    if row < 0:
        return 0.0
    v = matrix[row, col]
    return float(v) if np.isfinite(v) else 0.0


def _triggered(bar: dict | None, stop_price: float | None) -> bool:
    """盘中触及判定：跳空（open < stop）或触及（low ≤ stop）。"""
    if bar is None or stop_price is None:
        return False
    open_, low = bar.get("open"), bar.get("low")
    if open_ is not None and 0 < open_ < stop_price:
        return True
    return low is not None and low <= stop_price


def _finish(ctx, position: Position, bar: dict | None) -> None:
    """当日信息并入状态（供次日判定）。"""
    if bar is not None and position.stop is not None:
        eng_stops.post_day_update(
            position.stop, day_high=bar.get("high", np.nan), day_low=bar.get("low"),
        )


class HardStopModule:
    """硬止损 = 实际买入价 − mul × ATR(入场日含当根)。固定不动。"""

    def __init__(self, params: dict) -> None:
        self.atr_mul = float(params.get("atr_mul", 1.5))
        self.atr_period = int(params.get("atr_period", 20))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        price = ctx.panel.value(symbol, "close")
        atr = _atr(ctx, symbol, 0, self.atr_period)
        if price is None or atr <= 0:
            return None
        return price - self.atr_mul * atr

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        state = eng_stops.init_hard_stop(
            entry_price=fill.fill_price,
            atr_including_entry=_atr(ctx, fill.symbol, 0, self.atr_period),
            atr_mul=self.atr_mul,
        )
        state.highest_since_buy = max(high, fill.fill_price)
        return state

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        bar = _today_bar(ctx, position.symbol)
        stop = position.stop.stop_price if position.stop else None
        intent = None
        if _triggered(bar, stop):
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="intraday_stop", stop_price=stop, source="stop", reason="hard_stop",
            )
        _finish(ctx, position, bar)
        return intent


class ChandelierModule:
    """吊灯 = 买入以来最高价(T-1) − mul × ATR(T-1)。ratchet 变体只上移。"""

    ratchet = False

    def __init__(self, params: dict) -> None:
        self.atr_mul = float(params.get("atr_mul", 2.5))
        self.atr_period = int(params.get("atr_period", 20))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        price = ctx.panel.value(symbol, "close")
        atr = _atr(ctx, symbol, 0, self.atr_period)
        if price is None or atr <= 0:
            return None
        high_today = ctx.panel.value(symbol, "high") or price
        return max(high_today, price) - self.atr_mul * atr

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_chandelier(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, self.atr_period),
            atr_mul=self.atr_mul, ratchet=self.ratchet,
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        state = position.stop
        bar = _today_bar(ctx, position.symbol)
        if state is None:
            return None
        # 日内路径口径：T 日止损价用 T-1 状态（highest 与 ATR 都截至昨日）
        stop_t = eng_stops.daily_chandelier_stop(
            state, atr_through_yesterday=_atr(ctx, position.symbol, -1, self.atr_period),
            atr_mul=self.atr_mul,
        )
        if stop_t is not None:
            state.stop_price = stop_t
        intent = None
        if _triggered(bar, stop_t):
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="intraday_stop", stop_price=stop_t, source="stop",
                reason="ratchet" if self.ratchet else "chandelier",
            )
        _finish(ctx, position, bar)
        return intent


class RatchetModule(ChandelierModule):
    """棘轮吊灯：同吊灯公式，只上移不下移。"""
    ratchet = True


class MaStopModule:
    """均线止损：收盘跌破 SMA(n) → 尾盘离场（收盘确认型，fill_mode=tail）。

    日内路径口径不适用于收盘确认型（条件在收盘时刻已知）；
    heat 用 MA 参考线记近似（heat_approximate=True，§4.4 三类处理②）。
    """

    def __init__(self, params: dict) -> None:
        self.n = int(params.get("n", 20))

    def _ma(self, ctx, symbol: str) -> float | None:
        series = ctx.panel.series(symbol, "close")
        series = series[np.isfinite(series)]
        if len(series) < self.n:
            return None
        return float(series[-self.n:].mean())

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        return self._ma(ctx, symbol)

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_ma_stop(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20),
            ma_value=self._ma(ctx, fill.symbol),
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        bar = _today_bar(ctx, position.symbol)
        ma = self._ma(ctx, position.symbol)
        if position.stop is not None:
            position.stop.stop_price = ma  # 参考线逐日更新（heat 近似用）
        intent = None
        if bar is not None and ma is not None and bar["close"] < ma:
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="tail", source="stop", reason="ma_stop",
            )
        _finish(ctx, position, bar)
        return intent


class PctTrailingModule:
    """百分比回撤 trailing：持仓以来最高价(T-1) × (1−pct)。"""

    def __init__(self, params: dict) -> None:
        self.pct = float(params.get("pct", 0.1))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        price = ctx.panel.value(symbol, "close")
        high_today = ctx.panel.value(symbol, "high") or price
        if price is None:
            return None
        return max(high_today, price) * (1.0 - self.pct)

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_pct_trailing(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20), pct=self.pct,
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        state = position.stop
        bar = _today_bar(ctx, position.symbol)
        if state is None:
            return None
        stop_t = eng_stops.daily_pct_trailing_stop(state, pct=self.pct)
        if stop_t is not None:
            state.stop_price = stop_t
        intent = None
        if _triggered(bar, stop_t):
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="intraday_stop", stop_price=stop_t, source="stop",
                reason="pct_trailing",
            )
        _finish(ctx, position, bar)
        return intent


class DonchianExitModule:
    """唐奇安/前低止损：跌破最近 n 根最低价(T-1 窗口) → 盘中离场。"""

    def __init__(self, params: dict) -> None:
        self.n = int(params.get("n", 10))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        series = ctx.panel.series(symbol, "low")
        series = series[np.isfinite(series)]
        if len(series) < self.n:
            return None
        return float(series[-self.n:].min())

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_donchian(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20),
            channel_low=self.estimate_stop(ctx, fill.symbol),
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        state = position.stop
        bar = _today_bar(ctx, position.symbol)
        if state is None:
            return None
        # T-1 窗口的前低
        series = ctx.panel.series(position.symbol, "low")[:-1]  # 去掉当日
        series = series[np.isfinite(series)]
        stop_t = float(series[-self.n:].min()) if len(series) >= self.n else state.stop_price
        if stop_t is not None:
            state.stop_price = stop_t
        intent = None
        if _triggered(bar, stop_t):
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="intraday_stop", stop_price=stop_t, source="stop",
                reason="donchian_exit",
            )
        _finish(ctx, position, bar)
        return intent


class BreakevenModule:
    """保本止损：浮盈 > trigger_atr × ATR(入场) 后止损上移至买入价。"""

    def __init__(self, params: dict) -> None:
        self.trigger_atr = float(params.get("trigger_atr", 1.0))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        return None  # 初始无止损价——equal_risk 会跳过（组合实践：any_of 搭硬止损）

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_breakeven(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20),
            trigger_atr=self.trigger_atr,
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        state = position.stop
        bar = _today_bar(ctx, position.symbol)
        if state is None:
            return None
        stop_t = eng_stops.daily_breakeven_stop(state, entry_price=position.entry_price)
        if stop_t is not None:
            state.stop_price = stop_t
        intent = None
        if _triggered(bar, stop_t):
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="intraday_stop", stop_price=stop_t, source="stop",
                reason="breakeven",
            )
        _finish(ctx, position, bar)
        return intent


class TimeStopModule:
    """时间止损：持有 n 个交易日 → 尾盘强制离场（收盘确认型）。"""

    def __init__(self, params: dict) -> None:
        self.max_days = int(params.get("max_days", 20))

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        return None

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_time_stop(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20),
            max_days=self.max_days,
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        state = position.stop
        bar = _today_bar(ctx, position.symbol)
        if state is None:
            return None
        # 持有天数按交易日序号确定（评审 DS-P2-9：入场日=第 1 个交易日，
        # max_days=N 恰在第 N 个交易日离场；不再依赖自增计数器的 +1 错位）
        held = None
        dates = ctx.panel.dates
        if position.entry_date in dates:
            held = ctx.panel.upto - dates.index(position.entry_date) + 1
        if held is None:
            held = int(state.module_state.get("held_days", 0)) + 1
        intent = None
        if held >= self.max_days:
            intent = ExitOrderIntent(
                symbol=position.symbol, decision_date=ctx.date,
                fill_mode="tail", source="stop", reason="time_stop",
            )
        _finish(ctx, position, bar)
        return intent


class NoneRiskModule:
    """none：不止损（heat 记 None 并告警由报告层处理；benchmark 用）。"""

    def __init__(self, params: dict) -> None:
        pass

    def estimate_stop(self, ctx, symbol: str) -> float | None:
        return None

    def init_stop(self, ctx, fill) -> StopState:
        bar = _today_bar(ctx, fill.symbol)
        high = bar["high"] if bar and bar.get("high") else fill.fill_price
        return eng_stops.init_none(
            entry_price=fill.fill_price, entry_day_high=high,
            atr_including_entry=_atr(ctx, fill.symbol, 0, 20),
        )

    def evaluate(self, ctx, position: Position) -> ExitOrderIntent | None:
        _finish(ctx, position, _today_bar(ctx, position.symbol))
        return None


def register_position_risk_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="position_risk", name="hard_stop", version=1, factory=HardStopModule,
        params_schema={
            "atr_mul": {"type": "number", "default": 1.5, "min": 0.1, "max": 10},
            "atr_period": {"type": "integer", "default": 20, "min": 5, "max": 120},
        },
        description="硬止损（买入价 − mul×ATR 含当根）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="chandelier", version=1, factory=ChandelierModule,
        params_schema={
            "atr_mul": {"type": "number", "default": 2.5, "min": 0.5, "max": 10},
            "atr_period": {"type": "integer", "default": 20, "min": 5, "max": 120},
        },
        description="吊灯止损（T-1 最高价 − mul×ATR(T-1)）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="ratchet", version=1, factory=RatchetModule,
        params_schema={
            "atr_mul": {"type": "number", "default": 2.5, "min": 0.5, "max": 10},
            "atr_period": {"type": "integer", "default": 20, "min": 5, "max": 120},
        },
        description="棘轮吊灯（只上移）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="ma_stop", version=1, factory=MaStopModule,
        params_schema={"n": {"type": "integer", "default": 20, "min": 2, "max": 300}},
        description="均线止损（收盘确认型）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="pct_trailing", version=1, factory=PctTrailingModule,
        params_schema={"pct": {"type": "number", "default": 0.1, "min": 0.01, "max": 0.5}},
        description="百分比回撤 trailing",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="donchian_exit", version=1, factory=DonchianExitModule,
        params_schema={"n": {"type": "integer", "default": 10, "min": 3, "max": 120}},
        description="唐奇安/前低止损",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="breakeven", version=1, factory=BreakevenModule,
        params_schema={"trigger_atr": {"type": "number", "default": 1.0, "min": 0.1, "max": 10}},
        description="保本止损（浮盈达标后移至买入价）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="time_stop", version=1, factory=TimeStopModule,
        params_schema={"max_days": {"type": "integer", "default": 20, "min": 1, "max": 500}},
        description="时间止损（持有 N 日强制离场）",
    ))
    registry.register(ModuleSpec(
        slot="position_risk", name="none", version=1, factory=NoneRiskModule,
        params_schema={}, description="不止损（benchmark 用）",
    ))
