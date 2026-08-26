"""Golden-master tests for core/trend.py.

``ref_snapshot`` / ``ref_series`` are verbatim copies of the two legacy
implementations (strategy/trend_score_core snapshot and
market_view.compute_trend_indicator) frozen at the unification point.
The unified core must reproduce BOTH, plus the key invariant:
snapshot == last row of the series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import atr as _core_atr
from core.indicators import efficiency_ratio as _core_er
from core.trend import (
    calculate_trend_score_series,
    calculate_trend_score_snapshot,
    safe_float,
)

CFG = {
    "n_short": 5, "n_mid": 10, "n_long": 20, "atr_period": 20,
    "w_bias_short": 0.4, "w_bias_mid": 0.4, "w_bias_long": 0.2,
    "w_slope_short": 0.4, "w_slope_mid": 0.4, "w_slope_long": 0.2,
    "w_bias_norm": 0.5, "w_slope_norm": 0.5,
    "vol_ma_period": 20, "er_period": 10, "w_vol": 0.3, "w_er": 0.7,
}

SNAPSHOT_FIELDS = ["trend_score", "price_direction", "confidence", "atr", "price", "ma_mid"]
DETAIL_FIELDS = [
    "ma_mid", "atr", "bias_short", "bias_mid", "bias_long",
    "slope_short", "slope_mid", "slope_long", "bias_mix", "slope_mix",
    "norm_bias", "norm_slope", "vol_ma", "current_volume", "vol_ratio",
    "volume_factor", "er",
]


# ---------------------------------------------------------------------------
# Verbatim reference copies of the legacy implementations
# ---------------------------------------------------------------------------


def ref_snapshot(bars: pd.DataFrame, cfg: dict, *, fixed_atr=None, fixed_volume=None) -> dict:
    """Copy of strategy/trend_score_core.calculate_trend_score_snapshot."""
    n_short = int(cfg.get("n_short", 5))
    n_mid = int(cfg.get("n_mid", 10))
    n_long = int(cfg.get("n_long", 20))
    atr_period = int(cfg.get("atr_period", 20))
    min_bars = max(n_long, atr_period) + 2

    if bars.empty or len(bars) < min_bars:
        return {"ok": False, "reason": "insufficient_bars"}

    price = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    volume = pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0)
    calc_df = pd.DataFrame({"close": price, "high": high, "low": low, "volume": volume}).dropna(
        subset=["close", "high", "low"]
    )
    if len(calc_df) < min_bars:
        return {"ok": False, "reason": "invalid_bars_after_cleanup"}

    if fixed_atr is not None and fixed_atr > 0:
        atr_now = float(fixed_atr)
    else:
        atr_now = safe_float(_core_atr(calc_df, period=atr_period).iloc[-1], default=0.0)
    if atr_now <= 0:
        return {"ok": False, "reason": "invalid_atr"}

    weights_bias = np.array([cfg["w_bias_short"], cfg["w_bias_mid"], cfg["w_bias_long"]])
    weights_slope = np.array([cfg["w_slope_short"], cfg["w_slope_mid"], cfg["w_slope_long"]])

    bias_parts, slope_parts = [], []
    close_series = calc_df["close"]
    for n in (n_short, n_mid, n_long):
        ma_n = close_series.rolling(n, min_periods=n).mean().iloc[-1]
        bias_n = (close_series.iloc[-1] - ma_n) / atr_now if pd.notna(ma_n) else 0.0
        ema_n = close_series.ewm(span=n, adjust=False).mean()
        slope_n = 0.0
        if len(ema_n) >= 2:
            slope_n = (ema_n.iloc[-1] - ema_n.iloc[-2]) / (atr_now * n)
        bias_parts.append(safe_float(bias_n))
        slope_parts.append(safe_float(slope_n))

    bias_mix = float(np.dot(weights_bias, np.array(bias_parts)))
    slope_mix = float(np.dot(weights_slope, np.array(slope_parts)))
    norm_bias = float(np.tanh(bias_mix / 2.0) * 100.0)
    norm_slope = float(np.tanh(slope_mix) * 100.0)
    price_direction = cfg["w_bias_norm"] * norm_bias + cfg["w_slope_norm"] * norm_slope

    vol_ma = safe_float(calc_df["volume"].rolling(cfg["vol_ma_period"], min_periods=1).mean().iloc[-1], 0.0)
    if fixed_volume is not None and fixed_volume >= 0:
        current_volume = float(fixed_volume)
    else:
        current_volume = safe_float(calc_df["volume"].iloc[-1], 0.0)
    vol_ratio = (current_volume / vol_ma) if vol_ma > 0 else 0.0
    volume_factor = 1.0 if vol_ratio >= 3.0 else max(vol_ratio / 3.0, 0.0)
    er_now = float(np.clip(safe_float(_core_er(close_series, period=cfg["er_period"]).iloc[-1], 0.0), 0.0, 1.0))
    confidence = float((volume_factor ** cfg["w_vol"]) * (er_now ** cfg["w_er"]))
    trend_score = float(np.clip(price_direction * confidence, -100.0, 100.0))

    return {
        "ok": True,
        "trend_score": trend_score,
        "price_direction": price_direction,
        "confidence": confidence,
        "atr": atr_now,
        "price": safe_float(close_series.iloc[-1], 0.0),
        "ma_mid": safe_float(close_series.rolling(n_mid, min_periods=1).mean().iloc[-1], 0.0),
        "calc_details": {
            "ma_mid": safe_float(close_series.rolling(n_mid, min_periods=1).mean().iloc[-1], 0.0),
            "atr": atr_now,
            "bias_short": bias_parts[0], "bias_mid": bias_parts[1], "bias_long": bias_parts[2],
            "slope_short": slope_parts[0], "slope_mid": slope_parts[1], "slope_long": slope_parts[2],
            "bias_mix": bias_mix, "slope_mix": slope_mix,
            "norm_bias": norm_bias, "norm_slope": norm_slope,
            "vol_ma": vol_ma, "current_volume": current_volume,
            "vol_ratio": vol_ratio, "volume_factor": volume_factor, "er": er_now,
        },
    }


def ref_series(bars: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Copy of market_view.compute_trend_indicator's core computation."""
    n_short, n_mid, n_long = cfg["n_short"], cfg["n_mid"], cfg["n_long"]
    atr_period = cfg["atr_period"]
    min_bars = max(n_long, atr_period) + 2

    close = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    volume = pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0)
    calc_df = pd.DataFrame(
        {"close": close, "high": high, "low": low, "volume": volume}, index=bars.index
    ).dropna(subset=["close", "high", "low"])

    out = pd.DataFrame(
        np.nan, index=bars.index, columns=["trend_score", "price_direction", "confidence"]
    )
    if len(calc_df) < min_bars:
        return out

    close_series = calc_df["close"]
    atr_series = _core_atr(calc_df, period=atr_period)
    weights_bias = np.array([cfg["w_bias_short"], cfg["w_bias_mid"], cfg["w_bias_long"]])
    weights_slope = np.array([cfg["w_slope_short"], cfg["w_slope_mid"], cfg["w_slope_long"]])

    bias_parts, slope_parts = [], []
    for n in (n_short, n_mid, n_long):
        ma_n = close_series.rolling(n, min_periods=n).mean()
        bias_parts.append(((close_series - ma_n) / atr_series).fillna(0.0))
        ema_n = close_series.ewm(span=n, adjust=False).mean()
        slope_parts.append((ema_n.diff() / (atr_series * n)).fillna(0.0))

    bias_mix = weights_bias[0] * bias_parts[0] + weights_bias[1] * bias_parts[1] + weights_bias[2] * bias_parts[2]
    slope_mix = weights_slope[0] * slope_parts[0] + weights_slope[1] * slope_parts[1] + weights_slope[2] * slope_parts[2]
    norm_bias = np.tanh(bias_mix / 2.0) * 100.0
    norm_slope = np.tanh(slope_mix) * 100.0
    price_direction = cfg["w_bias_norm"] * norm_bias + cfg["w_slope_norm"] * norm_slope

    vol_ma = calc_df["volume"].rolling(cfg["vol_ma_period"], min_periods=1).mean()
    vol_ratio = calc_df["volume"] / vol_ma.replace(0, np.nan)
    volume_factor = (vol_ratio / 3.0).clip(lower=0.0, upper=1.0).fillna(0.0)
    er_now = _core_er(close_series, period=cfg["er_period"]).clip(lower=0.0, upper=1.0)
    confidence = (volume_factor ** cfg["w_vol"]) * (er_now ** cfg["w_er"])
    trend_score = (price_direction * confidence).clip(lower=-100.0, upper=100.0)

    valid = (pd.Series(range(1, len(calc_df) + 1), index=calc_df.index) >= min_bars) & (atr_series > 0)
    out.loc[calc_df.index, "trend_score"] = trend_score.where(valid)
    out.loc[calc_df.index, "price_direction"] = price_direction.where(valid)
    out.loc[calc_df.index, "confidence"] = confidence.where(valid)
    return out


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def _make_bars(seed: int, n: int = 300, jump: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.1, 1.5, n)
    closes = 100 + np.cumsum(steps)
    if jump:
        closes[n // 2 :] *= 0.85
    high = closes + np.abs(rng.normal(0, 0.5, n))
    low = closes - np.abs(rng.normal(0, 0.5, n))
    volume = np.abs(rng.normal(1e6, 3e5, n))
    return pd.DataFrame({"open": closes, "high": high, "low": low, "close": closes, "volume": volume})


def _assert_close(a: float, b: float, tol: float = 1e-9, label: str = "") -> None:
    assert abs(a - b) <= tol, f"{label}: {a} vs {b}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSnapshotMatchesLegacy:
    @pytest.mark.parametrize("seed,jump", [(1, False), (7, True), (42, False)])
    def test_snapshot_fields(self, seed, jump) -> None:
        bars = _make_bars(seed, jump=jump)
        old = ref_snapshot(bars, CFG)
        new = calculate_trend_score_snapshot(bars, CFG)
        assert old["ok"] is True and new["ok"] is True
        for f in SNAPSHOT_FIELDS:
            _assert_close(new[f], old[f], label=f)
        for f in DETAIL_FIELDS:
            _assert_close(new["calc_details"][f], old["calc_details"][f], label=f"calc_details.{f}")

    def test_snapshot_fixed_atr_volume(self) -> None:
        bars = _make_bars(11)
        old = ref_snapshot(bars, CFG, fixed_atr=3.21, fixed_volume=5e6)
        new = calculate_trend_score_snapshot(bars, CFG, fixed_atr=3.21, fixed_volume=5e6)
        assert old["ok"] is True and new["ok"] is True
        for f in SNAPSHOT_FIELDS:
            _assert_close(new[f], old[f], label=f)
        for f in DETAIL_FIELDS:
            _assert_close(new["calc_details"][f], old["calc_details"][f], label=f"calc_details.{f}")

    def test_insufficient_bars(self) -> None:
        bars = _make_bars(3, n=10)
        assert calculate_trend_score_snapshot(bars, CFG)["reason"] == "insufficient_bars"
        assert calculate_trend_score_snapshot(bars, CFG)["ok"] is False


class TestSeriesMatchesLegacy:
    @pytest.mark.parametrize("seed,jump", [(1, False), (7, True), (42, False)])
    def test_series_fields(self, seed, jump) -> None:
        bars = _make_bars(seed, jump=jump)
        old = ref_series(bars, CFG)
        new = calculate_trend_score_series(bars, CFG)
        for col in ("trend_score", "price_direction", "confidence"):
            for i in range(len(bars)):
                ov, nv = old[col].iloc[i], new[col].iloc[i]
                if pd.isna(ov) and pd.isna(nv):
                    continue
                assert pd.notna(ov) and pd.notna(nv), f"{col}[{i}] NaN mismatch: {ov} vs {nv}"
                _assert_close(nv, ov, label=f"{col}[{i}]")

    def test_trend_ma_columns(self) -> None:
        bars = _make_bars(5)
        new = calculate_trend_score_series(bars, CFG)
        score = new["trend_score"]
        assert abs(new["trend_ma5"].iloc[-1] - score.tail(5).mean()) < 1e-9
        assert abs(new["trend_ma10"].iloc[-1] - score.tail(10).mean()) < 1e-9


class TestSnapshotSeriesInvariant:
    @pytest.mark.parametrize("seed", [1, 7, 42, 99])
    def test_snapshot_equals_series_last_row(self, seed) -> None:
        """Core invariant: the snapshot IS the last row of the series."""
        bars = _make_bars(seed)
        series = calculate_trend_score_series(bars, CFG)
        snapshot = calculate_trend_score_snapshot(bars, CFG)
        last = series.iloc[-1]
        _assert_close(snapshot["trend_score"], last["trend_score"], label="trend_score")
        _assert_close(snapshot["price_direction"], last["price_direction"], label="price_direction")
        _assert_close(snapshot["confidence"], last["confidence"], label="confidence")
        _assert_close(snapshot["atr"], last["atr"], label="atr")

    def test_snapshot_equals_series_last_row_intraday(self) -> None:
        bars = _make_bars(13)
        series = calculate_trend_score_series(bars, CFG, fixed_atr=2.5, fixed_volume=8e6)
        snapshot = calculate_trend_score_snapshot(bars, CFG, fixed_atr=2.5, fixed_volume=8e6)
        last = series.iloc[-1]
        _assert_close(snapshot["trend_score"], last["trend_score"], label="trend_score")
        _assert_close(snapshot["atr"], last["atr"], label="atr")


class TestEdgeCases:
    def test_empty_bars(self) -> None:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = calculate_trend_score_series(empty, CFG)
        assert out.empty
        assert calculate_trend_score_snapshot(empty, CFG)["ok"] is False

    def test_all_nan_close(self) -> None:
        bars = _make_bars(5)
        bars["close"] = np.nan
        out = calculate_trend_score_series(bars, CFG)
        assert out["trend_score"].isna().all()

    def test_short_series(self) -> None:
        bars = _make_bars(6, n=15)
        out = calculate_trend_score_series(bars, CFG)
        assert out["trend_score"].isna().all()

    def test_warmup_region_is_nan(self) -> None:
        bars = _make_bars(8, n=100)
        out = calculate_trend_score_series(bars, CFG)
        min_bars = max(CFG["n_long"], CFG["atr_period"]) + 2
        assert out["trend_score"].iloc[: min_bars - 1].isna().all()
        assert pd.notna(out["trend_score"].iloc[min_bars - 1])

class TestSnapshotDirection:
    """自 test_smoke.py 合并（P2-25）：方向性冒烟断言。"""

    def test_bullish_scores_positive(self) -> None:
        import numpy as np
        import pandas as pd

        rows = 40
        closes = np.linspace(10.0, 20.0, rows)
        bars = pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=rows, freq="D"),
            "open": closes,
            "high": closes + 0.1,
            "low": closes - 0.1,
            "close": closes,
            "volume": np.full(rows, 100000.0),
        })
        result = calculate_trend_score_snapshot(bars, CFG)
        assert result["ok"] is True
        assert result["trend_score"] > 0

    def test_bearish_scores_negative(self) -> None:
        import numpy as np
        import pandas as pd

        rows = 40
        closes = np.linspace(20.0, 10.0, rows)
        bars = pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=rows, freq="D"),
            "open": closes,
            "high": closes + 0.1,
            "low": closes - 0.1,
            "close": closes,
            "volume": np.full(rows, 100000.0),
        })
        result = calculate_trend_score_snapshot(bars, CFG)
        assert result["ok"] is True
        assert result["trend_score"] < 0
