from __future__ import annotations

import pandas as pd

from core import indicators as core_ind
from core.trend import calculate_trend_score_snapshot, safe_float


def field_series(bars: pd.DataFrame, field: str) -> pd.Series:
    if field not in bars.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(bars[field], errors="coerce")

def latest_field(bars: pd.DataFrame, field: str) -> float | None:
    series = field_series(bars, field)
    if series.empty:
        return None
    return safe_float(series.iloc[-1])

def sma(bars: pd.DataFrame, field: str = "close", period: int = 20) -> tuple[float | None, dict]:
    series = field_series(bars, field).dropna()
    if len(series) < period:
        return None, {"reason": "insufficient_bars", "rows": len(series), "period": int(period)}
    window = series.tail(period)
    value = safe_float(core_ind.sma(series, period).iloc[-1])
    return value, {
        "field": field,
        "period": int(period),
        "window_values": [float(x) for x in window.tolist()],
        "value": value,
    }

def ema(bars: pd.DataFrame, field: str = "close", period: int = 20) -> tuple[float | None, dict]:
    series = field_series(bars, field).dropna()
    if len(series) < period:
        return None, {"reason": "insufficient_bars", "rows": len(series), "period": int(period)}
    ema_series = core_ind.ema(series, period, min_periods=0)
    value = safe_float(ema_series.iloc[-1])
    return value, {"field": field, "period": int(period), "value": value}

def atr(bars: pd.DataFrame, period: int = 20) -> tuple[float | None, dict]:
    if bars.empty or not {"high", "low", "close"}.issubset(bars.columns):
        return None, {"reason": "missing_ohlc"}
    atr_series = core_ind.atr(bars, period=period)
    value = safe_float(atr_series.iloc[-1])
    high = field_series(bars, "high")
    low = field_series(bars, "low")
    close = field_series(bars, "close")
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    window = tr.tail(period)
    return value, {
        "period": int(period),
        "tr_window": [float(x) for x in window.dropna().tolist()],
        "value": value,
    }

def bias(bars: pd.DataFrame, field: str = "close", period: int = 20) -> tuple[float | None, dict]:
    latest = latest_field(bars, field)
    ma_value, ma_trace = sma(bars, field=field, period=period)
    if latest is None or ma_value is None or ma_value == 0:
        return None, {"field": field, "period": int(period), "sma": ma_trace}
    value = (latest - ma_value) / ma_value
    return value, {"field": field, "period": int(period), "field_value": latest, "sma": ma_value, "value": value}

def bias_atr_normed(
    bars: pd.DataFrame,
    field: str = "close",
    period: int = 20,
    atr_period: int = 20,
) -> tuple[float | None, dict]:
    latest = latest_field(bars, field)
    ma_value, ma_trace = sma(bars, field=field, period=period)
    atr_value, atr_trace = atr(bars, period=atr_period)
    if latest is None or ma_value is None or atr_value is None or atr_value == 0:
        return None, {"field": field, "period": int(period), "sma": ma_trace, "atr": atr_trace}
    value = (latest - ma_value) / atr_value
    return value, {
        "field": field,
        "period": int(period),
        "atr_period": int(atr_period),
        "field_value": latest,
        "sma": ma_value,
        "atr": atr_value,
        "value": value,
    }

def rsi(bars: pd.DataFrame, field: str = "close", period: int = 14) -> tuple[float | None, dict]:
    series = field_series(bars, field).dropna()
    if len(series) < period + 1:
        return None, {"reason": "insufficient_bars", "rows": len(series), "period": int(period)}
    rsi_series = core_ind.rsi(series, period=period)
    value = safe_float(rsi_series.iloc[-1])
    return value, {"field": field, "period": int(period), "value": value}

def macd(
    bars: pd.DataFrame,
    field: str = "close",
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[dict[str, float | None], dict]:
    series = field_series(bars, field).dropna()
    min_rows = max(fast_period, slow_period) + signal_period
    if len(series) < min_rows:
        trace = {"reason": "insufficient_bars", "rows": len(series), "required": int(min_rows)}
        return {"line": None, "signal": None, "histogram": None}, trace
    out = core_ind.macd(series, fast_period=fast_period, slow_period=slow_period, signal_period=signal_period, warmup=True)
    values = {
        "line": safe_float(out["dif"].iloc[-1]),
        "signal": safe_float(out["dea"].iloc[-1]),
        "histogram": safe_float(out["hist"].iloc[-1]),
    }
    return values, {
        "field": field,
        "fast_period": int(fast_period),
        "slow_period": int(slow_period),
        "signal_period": int(signal_period),
        "values": values,
    }

def bollinger(
    bars: pd.DataFrame,
    field: str = "close",
    period: int = 20,
    std_mul: float = 2.0,
) -> tuple[dict[str, float | None], dict]:
    series = field_series(bars, field).dropna()
    if len(series) < period:
        trace = {"reason": "insufficient_bars", "rows": len(series), "period": int(period)}
        return {"upper": None, "middle": None, "lower": None}, trace
    out = core_ind.bollinger(series, period=period, std_mul=std_mul)
    middle = safe_float(out["mid"].iloc[-1])
    upper = safe_float(out["up"].iloc[-1])
    lower = safe_float(out["dn"].iloc[-1])
    if middle is None or upper is None or lower is None:
        values = {"upper": None, "middle": middle, "lower": None}
    else:
        values = {"upper": upper, "middle": middle, "lower": lower}
    window = series.tail(period)
    return values, {
        "field": field,
        "period": int(period),
        "std_mul": float(std_mul),
        "window_values": [float(x) for x in window.tolist()],
        "values": values,
    }

def momentum_return(bars: pd.DataFrame, field: str = "close", period: int = 20) -> tuple[float | None, dict]:
    series = field_series(bars, field).dropna()
    if len(series) <= period:
        return None, {"reason": "insufficient_bars", "rows": len(series), "period": int(period)}
    latest = safe_float(series.iloc[-1])
    previous = safe_float(series.iloc[-1 - period])
    if latest is None or previous is None or previous == 0:
        return None, {"reason": "invalid_values", "latest": latest, "previous": previous}
    value = latest / previous - 1.0
    return value, {"field": field, "period": int(period), "latest": latest, "previous": previous, "value": value}

def trend_score(bars: pd.DataFrame, cfg: dict | None = None) -> tuple[float | None, dict]:
    snapshot = calculate_trend_score_snapshot(bars=bars, cfg=cfg or {})
    value = safe_float(snapshot.get("trend_score")) if bool(snapshot.get("ok", False)) else None
    return value, dict(snapshot)

def trend_score_series(bars: pd.DataFrame, period: int, mode: str, cfg: dict | None = None) -> tuple[float | None, dict]:
    if len(bars) < period:
        return None, {"reason": "insufficient_bars", "rows": len(bars), "period": int(period)}
    values: list[float] = []
    for idx in range(max(0, len(bars) - period), len(bars)):
        value, _trace = trend_score(bars.iloc[: idx + 1].copy(), cfg=cfg)
        if value is None:
            return None, {"reason": "insufficient_trend_score_history", "period": int(period)}
        values.append(float(value))
    series = pd.Series(values, dtype=float)
    if mode == "ema":
        result = safe_float(series.ewm(span=period, adjust=False).mean().iloc[-1])
    else:
        result = safe_float(series.mean())
    return result, {"period": int(period), "mode": mode, "trend_score_values": values, "value": result}
