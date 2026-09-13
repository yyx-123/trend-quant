"""滚动锚定周/月K 与趋势值（core.rolling_bars）。

核心语义：第 t 日第 k 根 bar = 截至 t 的倒数第 k 个 D 交易日窗口
（D：周=5、月=22，右闭）；趋势值与「把同一截断序列喂给
calculate_trend_score_series」逐点一致；严格 PIT，无前视。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.rolling_bars import (
    ROLLING_BAR_DAYS,
    rolling_period_frame,
    rolling_period_trend_series,
    rolling_trend_cfg,
)
from core.trend import calculate_trend_score_series

K_BARS = 16  # 与 rolling_period_trend_series 默认 n_bars 一致


def _daily(rows: list[tuple]) -> pd.DataFrame:
    """rows: (date, open, high, low, close, volume)"""
    return pd.DataFrame(
        [
            {"time": d, "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for d, o, h, lo, c, v in rows
        ]
    )


def _random_walk(days: int = 800, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=days)
    ret = rng.normal(0.0005, 0.02, days)
    close = 10.0 * np.exp(np.cumsum(ret))
    open_ = close * (1 + rng.normal(0, 0.005, days))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, days))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, days))
    volume = rng.uniform(1e6, 3e6, days)
    return pd.DataFrame(
        {"time": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


class TestRollingPeriodFrame:
    def test_weekly_aggregation(self) -> None:
        rows = [
            (f"2026-08-{d:02d}", 10 + i, 10.5 + i, 9.8 + i, 10.2 + i, 100 * (i + 1))
            for i, d in enumerate(range(3, 17))  # 14 个连续日历日
        ]
        df = _daily(rows)
        # end_idx=9（第 10 根日K）：k=0 → 日K[5:10]，k=1 → 日K[0:5]
        frame = rolling_period_frame(df, 9, "1w")
        assert len(frame) == 2  # 10 // 5
        latest = frame.iloc[1]  # 升序，最新在后
        assert latest["time"] == pd.Timestamp(df["time"].iloc[9])
        assert latest["open"] == df["open"].iloc[5]
        assert latest["high"] == df["high"].iloc[5:10].max()
        assert latest["low"] == df["low"].iloc[5:10].min()
        assert latest["close"] == df["close"].iloc[9]
        assert latest["volume"] == df["volume"].iloc[5:10].sum()
        oldest = frame.iloc[0]
        assert oldest["time"] == pd.Timestamp(df["time"].iloc[4])
        assert oldest["volume"] == df["volume"].iloc[0:5].sum()

    def test_incomplete_first_window_dropped(self) -> None:
        df = _random_walk(days=12)
        frame = rolling_period_frame(df, 11, "1w")  # 12 根日K → 2 根完整周 bar
        assert len(frame) == 2
        assert frame.iloc[-1]["time"] == df["time"].iloc[11]

    def test_daily_period_rejected(self) -> None:
        with pytest.raises(ValueError):
            rolling_period_frame(_random_walk(days=30), 29, "1d")


class TestTrendEquivalence:
    """向量化结果 vs 逐日手工聚合 + canonical 公式。"""

    @pytest.mark.parametrize("period", ["1w", "1M"])
    def test_matches_canonical_series(self, period: str) -> None:
        df = _random_walk()
        cfg = rolling_trend_cfg(period)
        vec = rolling_period_trend_series(df, cfg, period=period)
        min_bars = max(cfg["n_long"], cfg["atr_period"]) + 2
        d = ROLLING_BAR_DAYS[period]
        first_valid = d * min_bars - 1
        sampled = [first_valid, first_valid + 1, 300, 500, 700, len(df) - 1]
        for idx in sampled:
            frame = rolling_period_frame(df, idx, period, n_bars=K_BARS)
            canonical = calculate_trend_score_series(frame, cfg).iloc[-1]
            a = vec.iloc[idx]
            assert not np.isnan(a), f"{period} idx={idx} 应为有效值"
            assert a == pytest.approx(canonical["trend_score"], abs=1e-10), f"{period} idx={idx}"

    @pytest.mark.parametrize("period", ["1w", "1M"])
    def test_warmup_boundary(self, period: str) -> None:
        df = _random_walk()
        cfg = rolling_trend_cfg(period)
        vec = rolling_period_trend_series(df, cfg, period=period)
        min_bars = max(cfg["n_long"], cfg["atr_period"]) + 2
        first_valid = ROLLING_BAR_DAYS[period] * min_bars - 1
        assert vec.iloc[:first_valid].isna().all()
        assert not np.isnan(vec.iloc[first_valid])

    def test_component_parity(self) -> None:
        """趋势值之外，中间量也逐点一致（防止碰巧相等）。"""
        df = _random_walk()
        cfg = rolling_trend_cfg("1M")
        vec = rolling_period_trend_series(df, cfg, period="1M")
        # 抽一天对比 canonical 的中间列
        idx = 700
        frame = rolling_period_frame(df, idx, "1M", n_bars=K_BARS)
        canonical = calculate_trend_score_series(frame, cfg).iloc[-1]
        assert vec.iloc[idx] == pytest.approx(canonical["trend_score"], abs=1e-10)
        # canonical 的 confidence/price_direction 乘积应等于趋势值（未触 ±100 clip 时）
        assert canonical["trend_score"] == pytest.approx(
            np.clip(canonical["price_direction"] * canonical["confidence"], -100, 100), abs=1e-10
        )


class TestPit:
    @pytest.mark.parametrize("period", ["1w", "1M"])
    def test_no_lookahead(self, period: str) -> None:
        df = _random_walk()
        cfg = rolling_trend_cfg(period)
        full = rolling_period_trend_series(df, cfg, period=period)
        idx = 600
        trunc = rolling_period_trend_series(df.iloc[: idx + 1], cfg, period=period)
        assert trunc.iloc[idx] == full.iloc[idx]

    def test_future_mutation_irrelevant(self) -> None:
        df = _random_walk()
        cfg = rolling_trend_cfg("1M")
        full = rolling_period_trend_series(df, cfg, period="1M")
        mutated = df.copy()
        mutated.loc[mutated.index[601:], ["open", "high", "low", "close", "volume"]] *= 3.0
        mut = rolling_period_trend_series(mutated, cfg, period="1M")
        pd.testing.assert_series_equal(full.iloc[:601], mut.iloc[:601])


class TestEdgeCases:
    def test_too_short_history_all_nan(self) -> None:
        df = _random_walk(days=100)
        vec = rolling_period_trend_series(df, rolling_trend_cfg("1M"), period="1M")
        assert vec.isna().all()

    def test_daily_period_rejected(self) -> None:
        with pytest.raises(ValueError):
            rolling_period_trend_series(_random_walk(days=100), {}, period="1d")

    def test_cfg_overrides(self) -> None:
        w = rolling_trend_cfg("1w")
        m = rolling_trend_cfg("1M")
        assert (w["atr_period"], w["vol_ma_period"], w["er_period"]) == (8, 8, 4)
        assert (m["atr_period"], m["vol_ma_period"], m["er_period"]) == (6, 6, 3)
        # 结构参数保持日K 默认
        assert (m["n_short"], m["n_mid"], m["n_long"]) == (3, 5, 8)
        assert m["w_bias_norm"] == 0.5
