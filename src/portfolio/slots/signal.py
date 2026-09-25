"""`signal` 信号插槽（详设 §5.2.2）。

protocol SignalModule: scan(ctx, members) -> list[SignalEvent]
输出是**事件流**（entry=建议买入，exit=建议卖出）；信号模块不决定买不买、
买多少——它只生产候选。exit 是"策略认为趋势结束"，与 position_risk
（"风控认为该止损"）并列，任一触发即卖。

性能形态：重指标（MACD/均线/突破）在 `prepare(panel)` 一次性向量化
预计算（(T,N) 矩阵；EMA/rolling 均为因果变换，无未来函数），scan 逐日
O(1) 查表。模块的逐日读取只到 ctx.panel.upto 行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from portfolio.registry import REGISTRY, ModuleSpec


@dataclass(frozen=True, slots=True)
class SignalEvent:
    symbol: str
    kind: str  # "entry" | "exit"
    date: date
    meta: dict = field(default_factory=dict)


def _close_df(panel) -> pd.DataFrame:
    return pd.DataFrame(
        panel.data["close"], index=pd.Index(panel.dates), columns=panel.symbols
    )


def _field_df(panel, name: str) -> pd.DataFrame:
    return pd.DataFrame(
        panel.data[name], index=pd.Index(panel.dates), columns=panel.symbols
    )


def _filled_df(panel, name: str) -> pd.DataFrame:
    """列内前向填充的面板切片（停牌日沿用最近可得价）。

    停牌日面板为 NaN，而
    ``rolling(n, min_periods=n)`` 只计非 NaN 观测——NaN 行**及其后 n-1 行**
    都取不到指标值，窗口恢复的那一根 bar 会被 ``~prev_above & above``
    判成"新交叉" → 停牌后凭空多出一笔买入（实证：同价序列抽掉一根 bar，
    干净序列 0 笔、缺口序列多出一笔 20 个交易日后的买单）。与
    ``backtester._precompute_atr`` 的 裁决同口径：先列内 ffill
    再算指标。MACD（ewm）不在此列——NaN 不会产生幻影交叉，且 ffill 会
    改变 EMA 递归权重。
    """
    return _field_df(panel, name).ffill()


def _last_true_idx(mask: np.ndarray) -> np.ndarray:
    """(T,N) bool → 每列截至当行最后一个 True 的行下标（无则 -1）。"""
    t_idx = np.arange(mask.shape[0])[:, None]
    hits = np.where(mask, t_idx, -1)
    return np.maximum.accumulate(hits, axis=0)


class MacdCrossSignal:
    """MACD 金叉 entry / 死叉 exit（详设 §5.2.0 示例同族；预热保护
    max(fast,slow)+signal 根有效 bar，与旧引擎 indicators.macd 同语义）。

    params: fast=12, slow=26, signal=9, use_exit=false（死叉不卖——exit 归
    止损管）, valid_days=5（交叉事件的有效候选窗口）。

    口径注记（K3-R2-注记-1）：本模块在 t≥34（valid≥35）即可发交叉；旧引擎/
    parity 驱动因交叉判定需前一日指标切片，实际最早可判日晚 1 根（i≥35）。
    属跨实现 1 日口径差——模块自身口径自洽，不对齐（parity 层按旧栈对齐）。
    """

    def __init__(self, params: dict) -> None:
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.signal = int(params.get("signal", 9))
        self.use_exit = bool(params.get("use_exit", False))
        self.valid_days = int(params.get("valid_days", 5))
        self.warmup = max(self.fast, self.slow) + self.signal
        self._ready = None

    def prepare(self, panel) -> None:
        close = _close_df(panel)
        fast = close.ewm(span=self.fast, adjust=False).mean()
        slow = close.ewm(span=self.slow, adjust=False).mean()
        dif = fast - slow
        dea = dif.ewm(span=self.signal, adjust=False).mean()
        valid = close.notna().cumsum()
        dif_v = dif.to_numpy(dtype=float)
        dea_v = dea.to_numpy(dtype=float)
        prev_dif = np.vstack([np.full((1, dif_v.shape[1]), np.nan), dif_v[:-1]])
        prev_dea = np.vstack([np.full((1, dea_v.shape[1]), np.nan), dea_v[:-1]])
        golden = (prev_dif <= prev_dea) & (dif_v > dea_v)
        dead = (prev_dif >= prev_dea) & (dif_v < dea_v)
        self._ready = {
            "dif": dif_v,
            "dea": dea_v,
            "valid": valid.to_numpy(),
            "last_golden": _last_true_idx(golden),
            "last_dead": _last_true_idx(dead),
        }

    def scan(self, ctx, members) -> list[SignalEvent]:
        r = self._ready
        t = ctx.panel.upto
        events: list[SignalEvent] = []
        today = ctx.date
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            if r["valid"][t, col] < self.warmup:
                continue
            dif_t, dea_t = r["dif"][t, col], r["dea"][t, col]
            if not np.isfinite(dif_t) or not np.isfinite(dea_t):
                continue
            lg = r["last_golden"][t, col]
            if lg >= 0 and (t - lg) < self.valid_days and dif_t > dea_t:
                events.append(SignalEvent(
                    symbol=m.symbol, kind="entry",
                    date=ctx.panel.date_at(int(lg)),
                    meta={"event_date": ctx.panel.date_at(int(lg)).isoformat()},
                ))
            elif self.use_exit:
                ld = r["last_dead"][t, col]
                if ld == t:  # 死叉当日才发 exit（exit 不持续候选）
                    events.append(SignalEvent(symbol=m.symbol, kind="exit", date=today, meta={}))
        return events


class MaCrossSignal:
    """收盘价上/下穿 SMA(n)（§5.2.0 示例：ma_cross@1）。"""

    def __init__(self, params: dict) -> None:
        self.n = int(params.get("n", 20))
        self.use_exit = bool(params.get("use_exit", True))
        self.valid_days = int(params.get("valid_days", 5))
        self._ready = None

    def prepare(self, panel) -> None:
        close = _filled_df(panel, "close")
        ma = close.rolling(self.n, min_periods=self.n).mean()
        above = (close > ma).to_numpy()
        prev_above = np.vstack([np.zeros((1, above.shape[1]), dtype=bool), above[:-1]])
        cross_up = (~prev_above) & above
        cross_dn = prev_above & (~above)
        self._ready = {
            "above": above,
            "last_up": _last_true_idx(cross_up),
            "last_dn": _last_true_idx(cross_dn),
        }

    def scan(self, ctx, members) -> list[SignalEvent]:
        r = self._ready
        t = ctx.panel.upto
        events: list[SignalEvent] = []
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            lu = r["last_up"][t, col]
            if lu >= 0 and r["above"][t, col] and (t - lu) < self.valid_days:
                events.append(SignalEvent(
                    symbol=m.symbol, kind="entry",
                    date=ctx.panel.date_at(int(lu)),
                    meta={"event_date": ctx.panel.date_at(int(lu)).isoformat()},
                ))
            elif self.use_exit and r["last_dn"][t, col] == t:
                events.append(SignalEvent(symbol=m.symbol, kind="exit", date=ctx.date, meta={}))
        return events


class ChannelBreakoutSignal:
    """海龟/唐奇安突破：close 创 entry_n 日新高入、创 exit_n 日新低出。"""

    def __init__(self, params: dict) -> None:
        self.entry_n = int(params.get("entry_n", 20))
        self.exit_n = int(params.get("exit_n", 10))
        self.valid_days = int(params.get("valid_days", 1))
        self._ready = None

    def prepare(self, panel) -> None:
        close = _filled_df(panel, "close")
        high = _filled_df(panel, "high")
        low = _filled_df(panel, "low")
        prev_high = high.rolling(self.entry_n, min_periods=self.entry_n).max().shift(1)
        prev_low = low.rolling(self.exit_n, min_periods=self.exit_n).min().shift(1)
        close_v = close.to_numpy(dtype=float)
        breakout = (close_v > prev_high.to_numpy(dtype=float))
        breakdown = (close_v < prev_low.to_numpy(dtype=float))
        self._ready = {
            "last_up": _last_true_idx(breakout),
            "last_dn": _last_true_idx(breakdown),
            "breakout": breakout,
        }

    def scan(self, ctx, members) -> list[SignalEvent]:
        r = self._ready
        t = ctx.panel.upto
        events: list[SignalEvent] = []
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            lu = r["last_up"][t, col]
            if lu >= 0 and (t - lu) < self.valid_days:
                events.append(SignalEvent(
                    symbol=m.symbol, kind="entry",
                    date=ctx.panel.date_at(int(lu)),
                    meta={"event_date": ctx.panel.date_at(int(lu)).isoformat()},
                ))
            if r["last_dn"][t, col] == t:
                events.append(SignalEvent(symbol=m.symbol, kind="exit", date=ctx.date, meta={}))
        return events


class High52wSignal:
    """52 周新高（George & Hwang 2004）：close 创 lookback 日新高。"""

    def __init__(self, params: dict) -> None:
        self.lookback = int(params.get("lookback", 252))
        self.valid_days = int(params.get("valid_days", 5))
        self.min_bars = int(params.get("min_bars", 60))
        self._ready = None

    def prepare(self, panel) -> None:
        close = _filled_df(panel, "close")
        rolling_max = close.rolling(self.lookback, min_periods=self.min_bars).max()
        close_v = close.to_numpy(dtype=float)
        at_high = close_v >= rolling_max.to_numpy(dtype=float)
        at_high &= np.isfinite(close_v)
        self._ready = {"last_hit": _last_true_idx(at_high)}

    def scan(self, ctx, members) -> list[SignalEvent]:
        t = ctx.panel.upto
        events: list[SignalEvent] = []
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            lh = self._ready["last_hit"][t, col]
            if lh >= 0 and (t - lh) < self.valid_days:
                events.append(SignalEvent(
                    symbol=m.symbol, kind="entry",
                    date=ctx.panel.date_at(int(lh)),
                    meta={"event_date": ctx.panel.date_at(int(lh)).isoformat()},
                ))
        return events


class AbsMomentumSignal:
    """绝对动量转正（双动量，Antonacci）：截面 top_n 且动量 > threshold 才入；
    持仓跌出 top 集或动量转负 → exit。"""

    def __init__(self, params: dict) -> None:
        self.lookback = int(params.get("lookback", 20))
        self.top_n = int(params.get("top_n", 1))
        self.threshold = float(params.get("threshold", 0.0))
        self._ready = None

    def prepare(self, panel) -> None:
        close = _filled_df(panel, "close")
        mom = close / close.shift(self.lookback) - 1.0
        self._ready = {"mom": mom.to_numpy(dtype=float)}

    def scan(self, ctx, members) -> list[SignalEvent]:
        t = ctx.panel.upto
        mom_row = self._ready["mom"][t]
        scores: list[tuple[str, float]] = []
        for m in members:
            col = ctx.panel.symbol_col(m.symbol)
            if col is None:
                continue
            v = mom_row[col]
            if np.isfinite(v):
                scores.append((m.symbol, float(v)))
        scores.sort(key=lambda x: -x[1])
        top = [s for s, v in scores[: self.top_n] if v > self.threshold]
        top_set = set(top)
        events = [
            SignalEvent(symbol=s, kind="entry", date=ctx.date,
                        meta={"event_date": ctx.date.isoformat(),
                              "momentum": dict(scores).get(s)})
            for s in top
        ]
        held = ctx.account.positions
        for m in members:
            if m.symbol in held and m.symbol not in top_set:
                events.append(SignalEvent(symbol=m.symbol, kind="exit", date=ctx.date,
                                          meta={"reason": "not_in_top"}))
        return events


class TrendScoreCrossSignal:
    """趋势值上/下穿阈值（生产指标 trend_daily 经 L1.5 直取）。"""

    def __init__(self, params: dict) -> None:
        self.threshold = float(params.get("threshold", 5.0))
        self.valid_days = int(params.get("valid_days", 5))
        self._series: dict[str, pd.Series] = {}

    def prepare_with_gateway(self, bound_gateway, symbols: list[str], since) -> None:
        self._series = bound_gateway.get_production_indicator(
            symbols=symbols, name="trend_score", since=since
        )

    def scan(self, ctx, members) -> list[SignalEvent]:
        events: list[SignalEvent] = []
        today = ctx.date
        for m in members:
            series = self._series.get(m.symbol)
            if series is None or series.empty:
                continue
            upto = series[series.index <= today]
            if len(upto) < 2:
                continue
            cur, prev = float(upto.iloc[-1]), float(upto.iloc[-2])
            if not np.isfinite(cur) or not np.isfinite(prev):
                continue
            cross_up_idx = None
            tail = upto.tail(self.valid_days + 1)
            vals = tail.to_numpy(dtype=float)
            for k in range(len(vals) - 1, 0, -1):
                if vals[k - 1] <= self.threshold < vals[k]:
                    cross_up_idx = k
                    break
            if cross_up_idx is not None and cur > self.threshold:
                cross_day = tail.index[cross_up_idx]
                events.append(SignalEvent(
                    symbol=m.symbol, kind="entry", date=cross_day,
                    meta={"event_date": cross_day.isoformat()},
                ))
            elif prev >= self.threshold > cur:
                events.append(SignalEvent(symbol=m.symbol, kind="exit", date=today, meta={}))
        return events


class AlwaysEntrySignal:
    """对所有成员每日发 entry（benchmark 用，如买入持有）。"""

    def __init__(self, params: dict) -> None:
        pass

    def scan(self, ctx, members) -> list[SignalEvent]:
        return [
            SignalEvent(symbol=m.symbol, kind="entry", date=ctx.date,
                        meta={"event_date": ctx.date.isoformat()})
            for m in members
        ]


class RandomEntrySignal:
    """随机入场（猴子基准，合法默认！）：每日随机抽 per_day 个成员发 entry。

    确定性：seed 固定 + 逐日派生 RNG——同配置同数据重跑结果位级一致。
    """

    def __init__(self, params: dict) -> None:
        self.seed = int(params.get("seed", 42))
        self.per_day = int(params.get("per_day", 3))

    def scan(self, ctx, members) -> list[SignalEvent]:
        if not members:
            return []
        rng = np.random.default_rng(self.seed * 1_000_003 + ctx.run_seed)
        k = min(self.per_day, len(members))
        picks = rng.choice(len(members), size=k, replace=False)
        return [
            SignalEvent(symbol=members[int(i)].symbol, kind="entry", date=ctx.date,
                        meta={"event_date": ctx.date.isoformat(), "random": True})
            for i in picks
        ]


def register_signal_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="signal", name="macd_cross", version=1, factory=MacdCrossSignal,
        params_schema={
            "fast": {"type": "integer", "default": 12, "min": 2, "max": 200},
            "slow": {"type": "integer", "default": 26, "min": 3, "max": 400},
            "signal": {"type": "integer", "default": 9, "min": 2, "max": 100},
            "use_exit": {"type": "boolean", "default": False},
            "valid_days": {"type": "integer", "default": 5, "min": 1, "max": 60},
        },
        description="MACD 金叉入/死叉出（use_exit=false 时只入）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="ma_cross", version=1, factory=MaCrossSignal,
        params_schema={
            "n": {"type": "integer", "default": 20, "min": 2, "max": 300},
            "use_exit": {"type": "boolean", "default": True},
            "valid_days": {"type": "integer", "default": 5, "min": 1, "max": 60},
        },
        description="收盘上/下穿 SMA(n)",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="channel_breakout", version=1, factory=ChannelBreakoutSignal,
        params_schema={
            "entry_n": {"type": "integer", "default": 20, "min": 5, "max": 120},
            "exit_n": {"type": "integer", "default": 10, "min": 3, "max": 60},
            "valid_days": {"type": "integer", "default": 1, "min": 1, "max": 20},
        },
        description="唐奇安突破（海龟）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="high_52w", version=1, factory=High52wSignal,
        params_schema={
            "lookback": {"type": "integer", "default": 252, "min": 60, "max": 600},
            "valid_days": {"type": "integer", "default": 5, "min": 1, "max": 20},
            "min_bars": {"type": "integer", "default": 60, "min": 20, "max": 252},
        },
        description="52 周新高（George & Hwang 2004）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="abs_momentum", version=1, factory=AbsMomentumSignal,
        params_schema={
            "lookback": {"type": "integer", "default": 20, "min": 5, "max": 300},
            "top_n": {"type": "integer", "default": 1, "min": 1, "max": 10},
            "threshold": {"type": "number", "default": 0.0},
        },
        description="截面动量 top_n 且绝对动量>阈值（双动量）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="trend_score_cross", version=1, factory=TrendScoreCrossSignal,
        params_schema={
            "threshold": {"type": "number", "default": 5.0},
            "valid_days": {"type": "integer", "default": 5, "min": 1, "max": 20},
        },
        description="趋势值上/下穿阈值（生产指标）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="always_entry", version=1, factory=AlwaysEntrySignal,
        params_schema={},
        description="每日对所有成员发 entry（benchmark 用）",
    ))
    registry.register(ModuleSpec(
        slot="signal", name="random_entry", version=1, factory=RandomEntrySignal,
        params_schema={
            "seed": {"type": "integer", "default": 42, "min": 0},
            "per_day": {"type": "integer", "default": 3, "min": 1, "max": 50},
        },
        description="随机入场（猴子基准）",
    ))
