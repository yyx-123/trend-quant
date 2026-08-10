"""Precomputed indicator store — cache-first reads with live-compute fallback.

Cache policy (master plan v1.1):
- The cache is an accelerator ONLY. Every read falls back to the live
  core implementations on miss/staleness — fallback is a permanent feature.
- Cached indicators are price/volume-derived and deterministic; stochastic
  or non-price indicators (e.g. random_uniform) never enter the cache.
- All cached rows are EOD. Intraday (not-yet-closed) bars are never
  persisted; the realtime overlay is layered on top at read time (P1.4).
- ATR has a single source: the ``atr`` column here (default param set
  locks atr_period=20). calc_stop_loss and every stop-related consumer
  must read from this column.
"""

from __future__ import annotations

import pandas as pd

from core import indicators as core_ind
from core.indicators import INDICATOR_FORMULA_VERSION
from core.strategy_config import get_strategy_config
from core.trend import TREND_FORMULA_VERSION, calculate_trend_score_series
from data.storage.db import get_db

INDICATOR_COLUMNS: tuple[str, ...] = (
    "atr", "vol_ma20", "er10",
    "sma5", "sma10", "sma20", "sma60", "sma120", "sma200",
    "ema5", "ema10", "ema20",
    "rsi14",
    "macd_dif", "macd_dea", "macd_hist",
    "boll_mid", "boll_up", "boll_dn",
    "rsi_avg_gain", "rsi_avg_loss",
    "macd_ema12", "macd_ema26",
)

TREND_COLUMNS: tuple[str, ...] = (
    "trend_score", "trend_ma5", "trend_ma10", "price_direction", "confidence",
)

# ---------------------------------------------------------------------------
# Frame builder (used by the builder pipeline and by tests)
# ---------------------------------------------------------------------------


def compute_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the indicator_daily frame for one symbol's K-line history."""
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else pd.Series(0.0, index=df.index)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    macd_out = core_ind.macd(close, warmup=True)
    boll_out = core_ind.bollinger(close)

    frame = pd.DataFrame(
        {
            "time": df["time"],
            "atr": core_ind.atr(df, period=20),
            "vol_ma20": core_ind.sma(volume, 20),
            "er10": core_ind.efficiency_ratio(close, 10),
            "sma5": core_ind.sma(close, 5),
            "sma10": core_ind.sma(close, 10),
            "sma20": core_ind.sma(close, 20),
            "sma60": core_ind.sma(close, 60),
            "sma120": core_ind.sma(close, 120),
            "sma200": core_ind.sma(close, 200),
            "ema5": core_ind.ema(close, 5),
            "ema10": core_ind.ema(close, 10),
            "ema20": core_ind.ema(close, 20),
            "rsi14": core_ind.rsi(close, 14),
            "macd_dif": macd_out["dif"],
            "macd_dea": macd_out["dea"],
            "macd_hist": macd_out["hist"],
            "boll_mid": boll_out["mid"],
            "boll_up": boll_out["up"],
            "boll_dn": boll_out["dn"],
            "rsi_avg_gain": avg_gain,
            "rsi_avg_loss": avg_loss,
            "macd_ema12": core_ind.ema(close, 12),
            "macd_ema26": core_ind.ema(close, 26),
        }
    )
    return frame


def compute_trend_frame(df: pd.DataFrame, trend_cfg: dict) -> pd.DataFrame:
    """Compute the trend_daily frame for one symbol's K-line history."""
    series = calculate_trend_score_series(df, trend_cfg)
    return pd.DataFrame(
        {
            "time": df["time"],
            "trend_score": series["trend_score"],
            "trend_ma5": series["trend_ma5"],
            "trend_ma10": series["trend_ma10"],
            "price_direction": series["price_direction"],
            "confidence": series["confidence"],
        }
    )


# ---------------------------------------------------------------------------
# Live fallback computation
# ---------------------------------------------------------------------------


def _rsi_components(close: pd.Series, indicator: str) -> pd.Series:
    delta = close.diff()
    if indicator == "rsi_avg_gain":
        return delta.clip(lower=0.0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return (-delta).clip(lower=0.0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()


def compute_live_series(bars: pd.DataFrame, indicator: str, trend_cfg: dict | None = None) -> pd.Series:
    """Compute one indicator series live from raw K-lines (fallback path)."""
    close = pd.to_numeric(bars["close"], errors="coerce")
    volume = pd.to_numeric(bars["volume"], errors="coerce") if "volume" in bars.columns else pd.Series(0.0, index=bars.index)

    if indicator == "atr":
        out = core_ind.atr(bars, period=20)
    elif indicator == "vol_ma20":
        out = core_ind.sma(volume, 20)
    elif indicator == "er10":
        out = core_ind.efficiency_ratio(close, 10)
    elif indicator.startswith("sma"):
        out = core_ind.sma(close, int(indicator[3:]))
    elif indicator.startswith("ema"):
        out = core_ind.ema(close, int(indicator[3:]))
    elif indicator == "rsi14":
        out = core_ind.rsi(close, 14)
    elif indicator in ("rsi_avg_gain", "rsi_avg_loss"):
        out = _rsi_components(close, indicator)
    elif indicator in ("macd_dif", "macd_dea", "macd_hist"):
        macd_out = core_ind.macd(close, warmup=True)
        out = macd_out[{"macd_dif": "dif", "macd_dea": "dea", "macd_hist": "hist"}[indicator]]
    elif indicator in ("macd_ema12", "macd_ema26"):
        out = core_ind.ema(close, int(indicator[-2:]))
    elif indicator in ("boll_mid", "boll_up", "boll_dn"):
        boll_out = core_ind.bollinger(close)
        out = boll_out[{"boll_mid": "mid", "boll_up": "up", "boll_dn": "dn"}[indicator]]
    elif indicator in TREND_COLUMNS:
        series = calculate_trend_score_series(bars, trend_cfg or get_strategy_config())
        out = series[indicator]
    else:
        raise ValueError(f"unknown indicator: {indicator}")

    out = out.copy()
    out.index = pd.to_datetime(bars["time"], errors="coerce")
    return out


# ---------------------------------------------------------------------------
# Cache-first reader
# ---------------------------------------------------------------------------


def _cache_fresh(symbol: str, indicator: str, db=None) -> bool:
    db = db or get_db()
    info = db.indicator_cache_info(symbol)
    market_end = db.get_market_data_summary(symbol).get("end")
    # 价格口径检查：行情内容版本（data_versions）在 qfq 原位重写时递增；
    # 缓存构建版本落后于当前行情版本即视为陈旧 —— 日期完全相同也一样。
    # 旧接口的 db（测试替身）没有版本方法时按 0 处理，退回纯日期口径。
    try:
        market_dv = db.get_data_version(db.market_data_version_name(symbol))
    except AttributeError:
        market_dv = 0
    if indicator in TREND_COLUMNS:
        if info["trend_rows"] == 0 or info["trend_version"] != TREND_FORMULA_VERSION:
            return False
        if market_dv > 0 and info.get("trend_data_version", 0) < market_dv:
            return False
        return bool(info["trend_last"] and market_end and str(info["trend_last"]) >= str(market_end))
    if info["indicator_rows"] == 0 or info["indicator_version"] != INDICATOR_FORMULA_VERSION:
        return False
    if market_dv > 0 and info.get("indicator_data_version", 0) < market_dv:
        return False
    return bool(info["indicator_last"] and market_end and str(info["indicator_last"]) >= str(market_end))


def get_series(symbol: str, indicator: str, db=None, since: str | None = None) -> pd.Series:
    """Return one indicator series for a symbol, cache-first with live fallback.

    ``since`` (ISO date) limits the cached rows returned — the live-fallback
    path is unaffected and always computes over full history.
    """
    db = db or get_db()
    symbol = str(symbol or "").strip().upper()

    if indicator in INDICATOR_COLUMNS + TREND_COLUMNS and _cache_fresh(symbol, indicator, db):
        if indicator in TREND_COLUMNS:
            frame = db.load_trend_daily(symbol, since=since)
        else:
            frame = db.load_indicator_daily(symbol)
            if since is not None and not frame.empty:
                frame = frame[frame["time"].astype(str) >= str(since)]
        if not frame.empty and indicator in frame.columns:
            out = pd.to_numeric(frame[indicator], errors="coerce")
            out.index = pd.to_datetime(frame["time"], errors="coerce")
            return out

    bars = db.load_market_data(symbol)
    if bars.empty:
        return pd.Series(dtype=float)
    return compute_live_series(bars, indicator)
