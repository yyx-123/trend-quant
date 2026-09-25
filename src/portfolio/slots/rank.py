"""`rank` 排序插槽（详设 §5.2.3）。

protocol RankModule: rank(ctx, candidates) -> list[SignalEvent]
纯排序（返回重排后的候选序列），不做筛选——筛选是 portfolio_risk 的职责
（rank 管"谁先谁后"，portfolio_risk 管"谁进谁出"）。
"""

from __future__ import annotations

import numpy as np

from core.indicators import efficiency_ratio
from portfolio.registry import REGISTRY, ModuleSpec
from portfolio.slots.signal import SignalEvent


def _event_day(ctx, event: SignalEvent) -> int:
    """事件发生日到今天的天数（自然日；meta.event_date 或 event.date）。

    口径（loop-review-ds4f R1-P3-7）：这里算的是**自然日**差，不是交易日差——
    对固定 ctx.date 而言自然日距离与交易日距离同序（等自然日差 ⇒ 同日），
    且 §5.2.3 只要"事件发生后天数升序"，排序结果与设计一致；注释此前写成
    "交易日距离"，与实现不符，按实现如实声明。
    """
    raw = event.meta.get("event_date") or event.date
    day = pd_timestamp(raw).date()
    return max((ctx.date - day).days, 0)


def pd_timestamp(value):
    import pandas as pd

    return pd.Timestamp(value)


class ByFreshnessRank:
    """信号新鲜度升序（越新越靠前）。默认实现——无脑但合法。"""

    def __init__(self, params: dict) -> None:
        pass

    def rank(self, ctx, candidates: list[SignalEvent]) -> list[SignalEvent]:
        return sorted(candidates, key=lambda e: (_event_day(ctx, e), e.symbol))


class ByErRank:
    """Kaufman ER 降序（趋势效率越高越靠前）。params: period=10。"""

    def __init__(self, params: dict) -> None:
        self.period = int(params.get("period", 10))

    def rank(self, ctx, candidates: list[SignalEvent]) -> list[SignalEvent]:
        import pandas as pd

        def er_of(e: SignalEvent) -> float:
            series = ctx.panel.series(e.symbol, "close")
            if len(series) < self.period + 1:
                return -1.0
            er = efficiency_ratio(pd.Series(series), self.period)
            v = float(er.iloc[-1]) if len(er) else 0.0
            return v if np.isfinite(v) else -1.0

        return sorted(candidates, key=lambda e: (-er_of(e), e.symbol))


class ByMomentumRank:
    """N 日动量降序。params: lookback=20。"""

    def __init__(self, params: dict) -> None:
        self.lookback = int(params.get("lookback", 20))

    def rank(self, ctx, candidates: list[SignalEvent]) -> list[SignalEvent]:
        def mom(e: SignalEvent) -> float:
            series = ctx.panel.series(e.symbol, "close")
            if len(series) <= self.lookback:
                return -np.inf
            base = series[-self.lookback - 1]
            last = series[-1]
            if not np.isfinite(base) or not np.isfinite(last) or base <= 0:
                return -np.inf
            return last / base - 1.0

        return sorted(candidates, key=lambda e: (-mom(e), e.symbol))


class BySlopeR2Rank:
    """Clenow 趋势质量：log 价回归年化斜率 × R²，降序。params: window=90。"""

    def __init__(self, params: dict) -> None:
        self.window = int(params.get("window", 90))

    def rank(self, ctx, candidates: list[SignalEvent]) -> list[SignalEvent]:
        def score(e: SignalEvent) -> float:
            series = ctx.panel.series(e.symbol, "close")[-self.window:]
            series = series[np.isfinite(series) & (series > 0)]
            if len(series) < self.window // 2:
                return -np.inf
            y = np.log(series)
            x = np.arange(len(y), dtype=float)
            if len(y) < 2:
                return -np.inf
            slope, intercept = np.polyfit(x, y, 1)
            y_hat = slope * x + intercept
            ss_res = float(np.sum((y - y_hat) ** 2))
            ss_tot = float(np.sum((y - y.mean()) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
            annualized = np.exp(slope * 252) - 1.0
            return annualized * r2

        return sorted(candidates, key=lambda e: (-score(e), e.symbol))


class RandomRank:
    """随机排序（猴子基准，合法默认）。确定性：seed + 日期派生。"""

    def __init__(self, params: dict) -> None:
        self.seed = int(params.get("seed", 42))

    def rank(self, ctx, candidates: list[SignalEvent]) -> list[SignalEvent]:
        rng = np.random.default_rng(self.seed * 1_000_003 + ctx.run_seed)
        order = rng.permutation(len(candidates))
        return [candidates[int(i)] for i in order]


def register_rank_modules(registry=REGISTRY) -> None:
    registry.register(ModuleSpec(
        slot="rank", name="by_freshness", version=1, factory=ByFreshnessRank,
        params_schema={}, description="信号新鲜度升序（默认）",
    ))
    registry.register(ModuleSpec(
        slot="rank", name="by_er", version=1, factory=ByErRank,
        params_schema={"period": {"type": "integer", "default": 10, "min": 2, "max": 120}},
        description="Kaufman ER 降序",
    ))
    registry.register(ModuleSpec(
        slot="rank", name="by_momentum", version=1, factory=ByMomentumRank,
        params_schema={"lookback": {"type": "integer", "default": 20, "min": 2, "max": 300}},
        description="N 日动量降序",
    ))
    registry.register(ModuleSpec(
        slot="rank", name="by_slope_r2", version=1, factory=BySlopeR2Rank,
        params_schema={"window": {"type": "integer", "default": 90, "min": 20, "max": 300}},
        description="Clenow 斜率×R² 降序",
    ))
    registry.register(ModuleSpec(
        slot="rank", name="random", version=1, factory=RandomRank,
        params_schema={"seed": {"type": "integer", "default": 42, "min": 0}},
        description="随机排序（合法默认）",
    ))
