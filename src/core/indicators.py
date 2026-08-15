"""Unified technical indicator library — the single implementation project-wide.

All functions are vectorized: pandas Series/DataFrame in, Series/DataFrame out.

Locked semantics (master plan v1.1):
- RSI: Wilder smoothing (alpha = 1/period)
- MACD histogram: (DIF - DEA) * 2  (China charting convention)
- BIAS: decimal ratio (presentation layer multiplies by 100 when needed)

Only price/volume-derived deterministic indicators belong here; anything
stochastic or non-price-derived (e.g. random_uniform) must never be cached
or unified into this module.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Bump when any formula in this module changes; indicator cache tables keyed
# by this version are rebuilt at startup (see data/indicator_store, future P1).
INDICATOR_FORMULA_VERSION = 1


def sma(series: pd.Series, period: int, min_periods: int | None = None) -> pd.Series:
    """Simple moving average; by default requires a full window."""
    if series.empty:
        return pd.Series(dtype=float)
    return series.rolling(period, min_periods=min_periods or period).mean()


def ema(series: pd.Series, span: int, min_periods: int = 0) -> pd.Series:
    """Exponential moving average (adjust=False).

    ``min_periods=0`` keeps warmup values (backtest behavior);
    ``min_periods=span`` suppresses the warmup region (chart behavior).
    """
    if series.empty:
        return pd.Series(dtype=float)
    return series.ewm(span=span, adjust=False, min_periods=min_periods).mean()


def atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Average True Range, SMA-smoothed with warmup allowed (min_periods=1)."""
    if df.empty:
        return pd.Series(dtype=float)
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def efficiency_ratio(series: pd.Series, period: int = 10) -> pd.Series:
    """Kaufman efficiency ratio: |net change| / sum of |steps|."""
    if series.empty:
        return pd.Series(dtype=float)
    change = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(period, min_periods=1).sum()
    er = change / volatility.replace(0, np.nan)
    return er.fillna(0.0)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI.

    Boundary rules (match the chart implementation): avg_loss == 0 with
    avg_gain > 0 -> 100; both zero -> 50.
    """
    if close.empty:
        return pd.Series(dtype=float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out


def macd(
    close: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    *,
    warmup: bool = True,
) -> pd.DataFrame:
    """MACD with histogram = (DIF - DEA) * 2.

    ``warmup=True`` starts the EMAs from the first bar (backtest behavior);
    ``warmup=False`` suppresses each EMA until its span is complete (chart
    behavior).
    """
    if close.empty:
        return pd.DataFrame({"dif": pd.Series(dtype=float), "dea": pd.Series(dtype=float), "hist": pd.Series(dtype=float)})
    fast = ema(close, fast_period, min_periods=0 if warmup else fast_period)
    slow = ema(close, slow_period, min_periods=0 if warmup else slow_period)
    dif = fast - slow
    dea = ema(dif, signal_period, min_periods=0 if warmup else signal_period)
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist})


def detect_macd_phase(
    closes: list[float | None],
    dates: list[str] | None = None,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict:
    """Detect the current MACD golden/dead-cross phase.

    金叉：DIF 上穿 DEA（hist = DIF - DEA 由负/零转正），转正当日为第 1 天；
    死叉：hist 由正/零转负，转负当日为第 1 天。最新一根 hist == 0 视为
    无相位（罕见，横盘极致情形）。

    Parameters
    ----------
    closes: close prices in chronological order (None allowed — dropped
        together with the aligned date). 盘中实时场景把合成K线的最新价
        追加在末尾即可让「盘中金叉/死叉」当天可见。
    dates: ISO date strings aligned with ``closes`` (for signal_date).

    Returns a dict with keys:
      phase: "golden" | "dead" | None
      days: int (the cross bar is day 1, counted in bars)
      change_pct: float (signal-day close → latest close % change)
      signal_date: str | None (ISO date of the cross bar)
    """
    default: dict = {"phase": None, "days": None, "change_pct": None, "signal_date": None}
    frame = pd.DataFrame({"close": pd.to_numeric(pd.Series(closes), errors="coerce")})
    frame["date"] = list(dates) if dates is not None else [None] * len(frame)
    frame = frame.dropna(subset=["close"]).reset_index(drop=True)
    # MACD 需要足够的预热历史才能让 EMA26/DEA9 收敛，低于此长度不报相位。
    min_bars = slow_period + signal_period + 10
    if len(frame) < min_bars:
        return default

    result = macd(frame["close"], fast_period, slow_period, signal_period, warmup=True)
    sign = (result["dif"] - result["dea"]).to_numpy(dtype=float)
    if not np.isfinite(sign[-1]) or sign[-1] == 0.0:
        return default
    phase = "golden" if sign[-1] > 0 else "dead"

    # Walk backwards while hist stays on the current side; the first bar of
    # the run is the cross bar (day 1). hist == 0 bars break the run.
    latest_idx = len(sign) - 1
    signal_idx = latest_idx
    for j in range(latest_idx - 1, -1, -1):
        value = sign[j]
        if not np.isfinite(value) or value == 0.0 or (value > 0) != (sign[-1] > 0):
            break
        signal_idx = j

    latest_close = float(frame["close"].iloc[latest_idx])
    signal_close = float(frame["close"].iloc[signal_idx])
    if latest_close <= 0 or signal_close <= 0:
        return default

    return {
        "phase": phase,
        "days": latest_idx - signal_idx + 1,
        "change_pct": round((latest_close / signal_close - 1.0) * 100.0, 2),
        "signal_date": frame["date"].iloc[signal_idx],
    }


def kline_mini(bars: pd.DataFrame, count: int = 30) -> dict:
    """Package the last ``count`` daily bars (OHLC + close MA5) for a mini chart.

    ``bars`` must contain ``open, high, low, close`` columns in chronological
    order. 盘中场景把当日合成K线追加在末尾再调用，末根即为实时K线。
    Returns ``{"kline": [{o,h,l,c}...], "kline_ma5": [...]}`` — MA5 与 kline
    对齐，不足 5 根的头部位置为 None。
    悬停提示所需的附加字段（存在时才附带）：``d`` 交易日期、``pct`` 当日
    涨跌幅（%）、``a`` 成交额。
    """
    empty: dict = {"kline": [], "kline_ma5": []}
    if bars is None or bars.empty:
        return empty
    # 涨跌幅在全序列上算好再截尾，保证尾部首根也有相对前一根的涨跌幅。
    pct_full = pd.to_numeric(bars["close"], errors="coerce").pct_change() * 100.0
    tail = bars.tail(count)
    close = pd.to_numeric(tail["close"], errors="coerce")
    ma5 = sma(close, 5, min_periods=5)
    pct = pct_full.loc[tail.index]
    has_time = "time" in tail.columns
    has_amount = "amount" in tail.columns
    candles: list[dict] = []
    ma5_values: list[float | None] = []
    for (_, row), ma_value, pct_value in zip(tail.iterrows(), ma5, pct):
        try:
            candle = {
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            }
        except (TypeError, ValueError):
            continue
        if not all(np.isfinite(v) for v in candle.values()):
            continue
        if has_time and pd.notna(row["time"]):
            candle["d"] = pd.Timestamp(row["time"]).date().isoformat()
        if np.isfinite(pct_value):
            candle["pct"] = round(float(pct_value), 2)
        if has_amount:
            amount = pd.to_numeric(pd.Series([row["amount"]]), errors="coerce").iloc[0]
            if np.isfinite(amount):
                candle["a"] = float(amount)
        candles.append(candle)
        ma5_values.append(float(ma_value) if np.isfinite(ma_value) else None)
    if not candles:
        return empty
    return {"kline": candles, "kline_ma5": ma5_values}


def macd_mini(bars: pd.DataFrame, count: int = 30) -> dict:
    """Package the last ``count`` bars' MACD (DIF/DEA/hist) for a mini chart.

    与 ``kline_mini`` 同一尾部窗口（根数一致、逐根对齐），EMA 预热口径与
    ``detect_macd_phase`` 相同（warmup=True，全历史预热后截取尾部）。
    盘中场景把当日合成K线追加在末尾再调用，末根即为实时值。
    """
    empty: dict = {"macd_dif": [], "macd_dea": [], "macd_hist": [], "macd_dates": []}
    if bars is None or bars.empty or "close" not in bars.columns:
        return empty
    close = pd.to_numeric(bars["close"], errors="coerce")
    if close.dropna().empty:
        return empty
    result = macd(close, warmup=True).tail(count)
    def _values(series: pd.Series) -> list[float | None]:
        return [float(v) if np.isfinite(v) else None for v in series]
    dates: list[str | None] = []
    if "time" in bars.columns:
        dates = [
            pd.Timestamp(v).date().isoformat() if pd.notna(v) else None
            for v in bars["time"].tail(count)
        ]
    return {
        "macd_dif": _values(result["dif"]),
        "macd_dea": _values(result["dea"]),
        "macd_hist": _values(result["hist"]),
        "macd_dates": dates,
    }


def bollinger(close: pd.Series, period: int = 20, std_mul: float = 2.0) -> pd.DataFrame:
    """Bollinger bands with population std (ddof=0)."""
    if close.empty:
        return pd.DataFrame({"mid": pd.Series(dtype=float), "up": pd.Series(dtype=float), "dn": pd.Series(dtype=float)})
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"mid": mid, "up": mid + std_mul * std, "dn": mid - std_mul * std})


def bias(close: pd.Series, period: int = 20) -> pd.Series:
    """(close - SMA(period)) / SMA(period) as a decimal ratio."""
    ma = sma(close, period)
    return (close - ma) / ma


def momentum_return(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple N-period return: series / series.shift(period) - 1."""
    if series.empty:
        return pd.Series(dtype=float)
    return series / series.shift(period) - 1.0
