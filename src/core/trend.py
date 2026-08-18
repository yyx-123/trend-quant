"""Trend score — canonical implementation (vectorized series form).

The series function ``calculate_trend_score_series`` is the single source of
truth; the snapshot ``calculate_trend_score_snapshot`` is a compatibility
wrapper that reads the LAST ROW of the same series, so the dashboard series
and single-point snapshots can never diverge by construction.

Formula (defaults from core.strategy_config):
  bias_n  = (close - MA_n) / ATR            n in (n_short, n_mid, n_long)
  slope_n = diff(EMA_n) / (ATR * n)
  norm_bias  = tanh(0.4*bias_s + 0.4*bias_m + 0.2*bias_l / 2) * 100
  norm_slope = tanh(0.4*slope_s + 0.4*slope_m + 0.2*slope_l) * 100
  price_direction = 0.5*norm_bias + 0.5*norm_slope
  confidence = min(vol_ratio/3, 1)^0.3 * ER(10)^0.7
  trend_score = clip(price_direction * confidence, -100, 100)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import atr as _atr
from core.indicators import efficiency_ratio as _er

# Bump when the trend-score formula changes; trend cache tables keyed by this
# version are rebuilt at startup (see data/indicator_store, future P1).
TREND_FORMULA_VERSION = 1

TREND_MA_PERIODS = (5, 10)

_SERIES_COLUMNS = [
    "trend_score",
    "trend_ma5",
    "trend_ma10",
    "price_direction",
    "confidence",
    "atr",
    "er",
    "vol_ratio",
]


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return float(value)
    except Exception:
        return default


def _min_bars(cfg: dict) -> int:
    n_long = int(cfg.get("n_long", 8))
    atr_period = int(cfg.get("atr_period", 20))
    return max(n_long, atr_period) + 2


def calculate_trend_score_series(
    bars: pd.DataFrame,
    cfg: dict,
    *,
    fixed_atr: float | None = None,
    fixed_volume: float | None = None,
) -> pd.DataFrame:
    """Compute the full trend-score series for OHLCV bars.

    Returns a DataFrame aligned to ``bars.index`` with columns:
    trend_score, trend_ma5, trend_ma10, price_direction, confidence,
    atr, er, vol_ratio, plus detail columns used by the snapshot wrapper.
    Rows before the warmup gate (bar >= min_bars and ATR > 0) are NaN.

    ``fixed_atr`` / ``fixed_volume`` replace the ATR / volume of the LAST
    bar only (intraday synthetic-bar support).
    """
    n_short = int(cfg.get("n_short", 3))
    n_mid = int(cfg.get("n_mid", 5))
    n_long = int(cfg.get("n_long", 8))
    atr_period = int(cfg.get("atr_period", 20))
    min_bars = _min_bars(cfg)

    columns = _SERIES_COLUMNS + [
        "ma_mid",
        "bias_short", "bias_mid", "bias_long",
        "slope_short", "slope_mid", "slope_long",
        "bias_mix", "slope_mix", "norm_bias", "norm_slope",
        "vol_ma", "volume_factor",
    ]
    out = pd.DataFrame(np.nan, index=bars.index, columns=columns, dtype="float64")

    close = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")
    volume_raw = bars["volume"] if "volume" in bars.columns else pd.Series(0.0, index=bars.index)
    volume = pd.to_numeric(volume_raw, errors="coerce").fillna(0.0)
    calc_df = pd.DataFrame(
        {"close": close, "high": high, "low": low, "volume": volume},
        index=bars.index,
    ).dropna(subset=["close", "high", "low"])

    if len(calc_df) < min_bars:
        return out

    close_series = calc_df["close"]
    atr_series = _atr(calc_df, period=atr_period)
    if fixed_atr is not None and fixed_atr > 0 and len(atr_series) > 0:
        atr_series = atr_series.copy()
        atr_series.iloc[-1] = float(fixed_atr)

    w_bias = np.array(
        [
            safe_float(cfg.get("w_bias_short", 0.4), 0.4),
            safe_float(cfg.get("w_bias_mid", 0.4), 0.4),
            safe_float(cfg.get("w_bias_long", 0.2), 0.2),
        ]
    )
    w_slope = np.array(
        [
            safe_float(cfg.get("w_slope_short", 0.4), 0.4),
            safe_float(cfg.get("w_slope_mid", 0.4), 0.4),
            safe_float(cfg.get("w_slope_long", 0.2), 0.2),
        ]
    )

    bias_parts: list[pd.Series] = []
    slope_parts: list[pd.Series] = []
    for n in (n_short, n_mid, n_long):
        ma_n = close_series.rolling(n, min_periods=n).mean()
        bias_parts.append(((close_series - ma_n) / atr_series).fillna(0.0))
        ema_n = close_series.ewm(span=n, adjust=False).mean()
        slope_parts.append((ema_n.diff() / (atr_series * n)).fillna(0.0))

    bias_mix = w_bias[0] * bias_parts[0] + w_bias[1] * bias_parts[1] + w_bias[2] * bias_parts[2]
    slope_mix = w_slope[0] * slope_parts[0] + w_slope[1] * slope_parts[1] + w_slope[2] * slope_parts[2]

    norm_bias = np.tanh(bias_mix / 2.0) * 100.0
    norm_slope = np.tanh(slope_mix) * 100.0
    price_direction = (
        safe_float(cfg.get("w_bias_norm", 0.5), 0.5) * norm_bias
        + safe_float(cfg.get("w_slope_norm", 0.5), 0.5) * norm_slope
    )

    vol_ma_period = int(cfg.get("vol_ma_period", 20))
    er_period = int(cfg.get("er_period", 10))
    vol_ma = calc_df["volume"].rolling(vol_ma_period, min_periods=1).mean()

    current_volume = calc_df["volume"].copy()
    if fixed_volume is not None and fixed_volume >= 0 and len(current_volume) > 0:
        current_volume.iloc[-1] = float(fixed_volume)

    vol_ratio = current_volume / vol_ma.replace(0, np.nan)
    volume_factor = (vol_ratio / 3.0).clip(lower=0.0, upper=1.0).fillna(0.0)
    er_now = _er(close_series, period=er_period).clip(lower=0.0, upper=1.0)

    confidence = (volume_factor ** safe_float(cfg.get("w_vol", 0.3), 0.3)) * (
        er_now ** safe_float(cfg.get("w_er", 0.7), 0.7)
    )
    trend_score = (price_direction * confidence).clip(lower=-100.0, upper=100.0)

    valid = (pd.Series(range(1, len(calc_df) + 1), index=calc_df.index) >= min_bars) & (
        atr_series > 0
    )

    def _gated(series: pd.Series) -> pd.Series:
        full = pd.Series(np.nan, index=bars.index, dtype="float64")
        full.loc[calc_df.index] = series.where(valid)
        return full

    score_full = _gated(trend_score)
    out["trend_score"] = score_full
    for period in TREND_MA_PERIODS:
        out[f"trend_ma{period}"] = score_full.rolling(period, min_periods=period).mean()
    out["price_direction"] = _gated(price_direction)
    out["confidence"] = _gated(confidence)
    out["atr"] = _gated(atr_series)
    out["er"] = _gated(er_now)
    out["vol_ratio"] = _gated(vol_ratio)

    # Detail columns (used by the snapshot wrapper's calc_details).
    out["ma_mid"] = _gated(close_series.rolling(n_mid, min_periods=1).mean())
    out["bias_short"], out["bias_mid"], out["bias_long"] = (_gated(s) for s in bias_parts)
    out["slope_short"], out["slope_mid"], out["slope_long"] = (_gated(s) for s in slope_parts)
    out["bias_mix"] = _gated(bias_mix)
    out["slope_mix"] = _gated(slope_mix)
    out["norm_bias"] = _gated(norm_bias)
    out["norm_slope"] = _gated(norm_slope)
    out["vol_ma"] = _gated(vol_ma)
    out["volume_factor"] = _gated(volume_factor)
    return out


def calculate_trend_score_snapshot(
    bars: pd.DataFrame,
    cfg: dict,
    *,
    fixed_atr: float | None = None,
    fixed_volume: float | None = None,
) -> dict:
    """Single-bar trend snapshot — the last row of the canonical series.

    Preserves the historical dict contract (ok/reason/trend_score/atr/
    price/ma_mid/calc_details) used by rule-backtest and intraday callers.
    """
    min_bars = _min_bars(cfg)
    if bars.empty or len(bars) < min_bars:
        return {
            "ok": False,
            "reason": "insufficient_bars",
            "trend_score": 0.0,
            "price_direction": 0.0,
            "confidence": 0.0,
            "atr": 0.0,
            "price": 0.0,
            "ma_mid": 0.0,
            "calc_details": {"rows": int(len(bars)), "required": int(min_bars)},
        }

    close_numeric = pd.to_numeric(bars["close"], errors="coerce")
    high_numeric = pd.to_numeric(bars["high"], errors="coerce")
    low_numeric = pd.to_numeric(bars["low"], errors="coerce")
    cleaned = int(pd.DataFrame({"close": close_numeric, "high": high_numeric, "low": low_numeric}).dropna().shape[0])
    if cleaned < min_bars:
        return {
            "ok": False,
            "reason": "invalid_bars_after_cleanup",
            "trend_score": 0.0,
            "price_direction": 0.0,
            "confidence": 0.0,
            "atr": 0.0,
            "price": 0.0,
            "ma_mid": 0.0,
            "calc_details": {"rows": cleaned, "required": int(min_bars)},
        }

    series = calculate_trend_score_series(
        bars, cfg, fixed_atr=fixed_atr, fixed_volume=fixed_volume
    )
    last = series.iloc[-1]
    last_price = safe_float(close_numeric.iloc[-1], 0.0)

    if pd.isna(last["atr"]) or last["atr"] <= 0:
        return {
            "ok": False,
            "reason": "invalid_atr",
            "trend_score": 0.0,
            "price_direction": 0.0,
            "confidence": 0.0,
            "atr": 0.0,
            "price": last_price,
            "ma_mid": 0.0,
            "calc_details": {"atr": safe_float(last["atr"], 0.0)},
        }

    calc_details = {
        "price": last_price,
        "ma_mid": safe_float(last["ma_mid"]),
        "atr": safe_float(last["atr"]),
        "bias_short": safe_float(last["bias_short"]),
        "bias_mid": safe_float(last["bias_mid"]),
        "bias_long": safe_float(last["bias_long"]),
        "slope_short": safe_float(last["slope_short"]),
        "slope_mid": safe_float(last["slope_mid"]),
        "slope_long": safe_float(last["slope_long"]),
        "bias_mix": safe_float(last["bias_mix"]),
        "slope_mix": safe_float(last["slope_mix"]),
        "norm_bias": safe_float(last["norm_bias"]),
        "norm_slope": safe_float(last["norm_slope"]),
        "vol_ma": safe_float(last["vol_ma"]),
        "current_volume": safe_float(
            float(fixed_volume)
            if fixed_volume is not None and fixed_volume >= 0
            else pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0).iloc[-1]
        ),
        "vol_ratio": safe_float(last["vol_ratio"]),
        "volume_factor": safe_float(last["volume_factor"]),
        "er": safe_float(last["er"]),
    }

    return {
        "ok": True,
        "reason": "ok",
        "trend_score": safe_float(last["trend_score"]),
        "price_direction": safe_float(last["price_direction"]),
        "confidence": safe_float(last["confidence"]),
        "atr": safe_float(last["atr"]),
        "price": last_price,
        "ma_mid": safe_float(last["ma_mid"]),
        "calc_details": calc_details,
    }


def _detect_trend_phase(
    trend_scores: list[float | None],
    trend_ma5: list[float | None],
    closes: list[float | None],
    dates: list[str],
) -> dict:
    """Detect the current trend phase and its start date.

    Uses the last bar's trend_score and MA5 to determine the current
    phase ("start" / "end"), then walks backwards to find the transition
    bar where this phase began.  Returns None if the current bar is in
    neither state.

    - 趋势启动 (start): trend_score >= 5  AND  trend_ma5 >= 0
    - 趋势结束 (end):   trend_score <= -5 AND  trend_ma5 <= 0

    Returns a dict with keys:
      phase: "start" | "end" | None
      days:  int (the transition bar is day 1)
      change_pct: float (transition close → latest close % change)
      signal_date: str (ISO date of the transition bar)
    """
    default: dict = {
        "phase": None,
        "days": None,
        "change_pct": None,
        "signal_date": None,
    }
    n = len(trend_scores)
    if n < 5:
        return default

    # Scan backwards from the latest bar to find the most recent signal.
    # Skip bars that are NEITHER (don't satisfy either condition).
    latest_idx = n - 1
    phase = None
    scan_idx = -1
    for i in range(n - 1, 3, -1):
        ts = trend_scores[i]
        ma5 = trend_ma5[i]
        if ts is None or ma5 is None:
            continue
        if ts >= 5 and ma5 >= 0:
            phase = "start"
            scan_idx = i
            break
        if ts <= -5 and ma5 <= 0:
            phase = "end"
            scan_idx = i
            break
    if phase is None:
        return default  # no signal found at all

    latest_close = closes[latest_idx] if latest_idx < len(closes) else None
    if latest_close is None or latest_close <= 0:
        return default

    # Walk backwards from scan_idx to find where this phase started
    # (the transition point — first bar where the condition became true).
    signal_idx = scan_idx
    for j in range(scan_idx - 1, 3, -1):
        prev_ts = trend_scores[j]
        prev_ma5 = trend_ma5[j]
        if prev_ts is None or prev_ma5 is None:
            break
        if phase == "start":
            if prev_ts >= 5 and prev_ma5 >= 0:
                signal_idx = j  # still in same phase — move start earlier
            else:
                break  # phase started at signal_idx
        else:  # phase == "end"
            if prev_ts <= -5 and prev_ma5 <= 0:
                signal_idx = j
            else:
                break

    signal_close = closes[signal_idx] if signal_idx < len(closes) else None
    if signal_close is None or signal_close <= 0:
        return default

    # Days: transition bar is day 1, counted to the latest bar.
    days = latest_idx - signal_idx + 1
    change_pct = round((latest_close / signal_close - 1.0) * 100.0, 2)
    signal_date = dates[signal_idx] if signal_idx < len(dates) else None

    return {
        "phase": phase,
        "days": days,
        "change_pct": change_pct,
        "signal_date": signal_date,
    }

