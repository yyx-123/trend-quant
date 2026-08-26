"""Intraday (real-time) market data service.

All functions operate **in-memory only** — they never write to the
database.  The synthetic intraday bars are constructed from TickFlow
real-time quotes and merged with historical daily bars for trend-score
calculation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from audit.app_logger import get_logger
from core.calendar import is_past_market_open, is_realtime_available, market_now
from core.indicators import atr as _compute_atr
from core.indicators import detect_macd_phase, kline_mini, macd_mini
from core.numfmt import number_or_none as _number
from core.trend import _detect_trend_phase, calculate_trend_score_snapshot, safe_float
from data.service import DataService, get_data_service
from data.storage.db import Database
from services.dashboard_common import DISPLAY_DAYS as _DISPLAY_DAYS
from services.dashboard_common import assign_strength as _assign_strength
from services.dashboard_common import key_tuple as _key_tuple
from services.dashboard_common import ma5 as _ma5
from services.dashboard_common import macd_counts as _macd_counts
from services.dashboard_common import priority as _priority

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# synthetic bar construction
# ---------------------------------------------------------------------------

def quote_trade_date(quote: dict) -> date | None:
    """Extract the trading date carried by a real-time quote.

    Handles ISO-ish strings and epoch seconds/milliseconds; returns None
    when the quote carries no parseable timestamp.
    """

    raw = quote.get("ts")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        epoch = float(text)
    except ValueError:
        epoch = None
    if epoch is not None and epoch > 1e9:
        seconds = epoch / 1000.0 if epoch > 1e12 else epoch
        market_tz = market_now().tzinfo
        return datetime.fromtimestamp(seconds, tz=UTC).astimezone(market_tz).date()
    parsed = pd.to_datetime(text, errors="coerce")
    if parsed is None or pd.isna(parsed):
        return None
    ts = pd.Timestamp(parsed)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(market_now().tzinfo)
    return ts.date()

def is_quote_fresh(quote: dict) -> bool:
    """True when the quote may represent today's session.

    A quote whose trade date parses to an earlier day — e.g. a suspended
    （停牌） symbol's last trade, or a vendor-stale snapshot — must never be
    synthesized into a "today" bar. Quotes with no parseable timestamp are
    allowed through (``price is None`` remains the hard failure mode and is
    handled by callers); the session gates already restrict synthesis to
    trading days past the open.
    """
    trade_date = quote_trade_date(quote)
    if trade_date is None:
        return True
    fresh = trade_date >= market_now().date()
    if not fresh:
        logger.info(
            "Discarding stale quote (trade date %s, today %s) for %s",
            trade_date, market_now().date(), quote.get("symbol"),
        )
    return fresh

def build_synthetic_bar(quote: dict, prev_volume: float) -> dict:
    """Build a single synthetic daily bar from a real-time quote.

    Parameters
    ----------
    quote: TickFlow quote dict with keys ``open, high, low, price``.
    prev_volume: previous trading day's volume (used as today's
        approximate volume because intraday volume is incomplete).

    Returns
    -------
    dict with keys ``time, open, high, low, close, volume, amount``
    suitable for appending to a historical daily-bar DataFrame.
    """
    quote_open = _number(quote.get("open")) or 0.0
    quote_high = _number(quote.get("high")) or 0.0
    quote_low = _number(quote.get("low")) or 0.0
    quote_price = _number(quote.get("price")) or 0.0
    quote_amount = _number(quote.get("amount")) or 0.0

    # Missing/zero OHLC fields (e.g. suspended symbols) must not collapse
    # the bar to open=0/low=0 — fall back to the last price.
    if quote_open <= 0:
        quote_open = quote_price
    if quote_high <= 0:
        quote_high = max(quote_open, quote_price)
    if quote_low <= 0:
        quote_low = min(quote_open, quote_price)

    return {
        "time": market_now().replace(tzinfo=None),  # naive Beijing wall time, matching DB bars
        "open": quote_open,
        "high": max(quote_high, quote_open, quote_price),
        "low": min(quote_low, quote_open, quote_price),
        "close": quote_price,
        "volume": float(prev_volume) if prev_volume > 0 else 0.0,
        "amount": quote_amount,
    }

# ---------------------------------------------------------------------------
# intraday overlay — shared by the web daily-K endpoint and MCP symbol_detail
# ---------------------------------------------------------------------------

def _clean_daily_hist(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a daily-bar frame for intraday computations (time/OHLCV)."""
    if df.empty:
        return df
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = (
        df.dropna(subset=["time", "open", "high", "low", "close"])
        .sort_values("time")
        .reset_index(drop=True)
    )
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def has_persisted_today_bar(hist: pd.DataFrame) -> bool:
    """True when *hist* already contains today's persisted bar.

    「当日K线已落库 → DB 数据优先」的唯一实现：intraday overlay 与
    止损/持仓路径共用，任何第三处需要此判断时也必须复用本函数。
    """
    if hist.empty or "time" not in hist.columns:
        return False
    last = pd.to_datetime(hist["time"], errors="coerce").max()
    if pd.isna(last):
        return False
    return pd.Timestamp(last).date() >= market_now().date()

def build_intraday_overlay(
    symbol: str,
    hist: pd.DataFrame,
    trend_config: dict,
    data_service: DataService | None = None,
) -> dict | None:
    """Build today's synthetic-bar overlay for a daily-K payload, or None.

    Single implementation behind both the web daily-K endpoint
    (``routers/market_view``) and the MCP ``symbol_detail`` tool, so the two
    surfaces can never drift apart:

    - gated on ``is_past_market_open`` — at/past the 9:30 open today's bar
      must be present; this also covers the post-close window before the
      daily write job persists it;
    - returns None when today's bar is already persisted (DB data wins),
      outside the gate, or on any quote/compute failure (silent EOD
      fallback, warning logged);
    - the synthetic bar's volume prefers the quote's own figure (post-close
      it is the full-day actual), falling back to the previous day's
      approximation while the session is still running.

    Returns ``{"date", "ts", "bar", "trend"}`` where ``bar`` is the
    synthetic bar dict and ``trend`` the intraday trend snapshot.
    """
    if not is_past_market_open():
        return None
    try:
        hist = _clean_daily_hist(hist)
        if hist.empty or has_persisted_today_bar(hist):
            # Today's bar is already persisted — nothing to overlay.
            return None
        # 进程级单例（P2-11）：不再每次新建/泄漏 client，也无需 close。
        ds = data_service or get_data_service()
        quote = ds.fetch_latest_quote(symbol)
        if not quote or quote.get("price") is None or not is_quote_fresh(quote):
            return None
        result = compute_intraday_trend_score(hist, quote, trend_config)
        if not result.get("ok"):
            return None
        prev_vol = safe_float(hist["volume"].iloc[-1], 0.0) if len(hist) else 0.0
        synth = build_synthetic_bar(quote, prev_vol)
        quote_vol = safe_float(quote.get("volume"), 0.0)
        if quote_vol > 0:
            synth["volume"] = quote_vol
        return {
            "date": str(market_now().date()),
            "ts": market_now().isoformat(),
            # 收盘后为 True：合成K线来自收盘报价快照（日K补库前的估算），
            # 供前端/MCP 正确标注「收盘估算」而非「盘中实时」（不依赖
            # 消费方时区去解析 ts）。
            "post_close": not is_realtime_available(),
            "bar": synth,
            "trend": {
                "score": result["trend_score"],
                "price_direction": result["price_direction"],
                "confidence": result["confidence"],
                "atr": result["atr"],
                "price": result["price"],
                "ma_mid": result["ma_mid"],
                "calc_details": result.get("calc_details", {}),
            },
        }
    except Exception as exc:
        # Fall back to EOD data if the intraday fetch fails.
        logger.warning("Intraday overlay failed for %s; falling back to EOD: %s", symbol, exc)
        return None

# ---------------------------------------------------------------------------
# intraday trend score
# ---------------------------------------------------------------------------

def compute_intraday_trend_cached(
    symbol: str,
    quote: dict,
    tail_bars: pd.DataFrame,
    cache_row: dict | None,
    trend_config: dict,
) -> dict:
    """Compute today's trend score from cached EOD state + realtime quote.

    Exact-equivalent to ``compute_intraday_trend_score`` on full history, but
    O(1): bias uses tail closes, slope recurses EMA from the cached
    ``ema_s/ema_m/ema_l`` state columns, ATR/ER/vol_ma use short tails, and the
    ATR anchor is the cached yesterday value (fixed_atr semantics).

    Returns the same dict shape as ``compute_intraday_trend_score``.
    ``cache_row`` must contain atr/ema_s/ema_m/ema_l; when None the caller
    should fall back to the full-history path.
    """
    if tail_bars.empty or cache_row is None:
        return {"ok": False, "reason": "missing_cache", "is_intraday": True}

    close = pd.to_numeric(tail_bars["close"], errors="coerce").dropna()
    volume = pd.to_numeric(tail_bars["volume"], errors="coerce").fillna(0.0)
    if close.empty:
        return {"ok": False, "reason": "insufficient_tail", "is_intraday": True}

    # 锚点新鲜度校验：缓存锚点行（EMA/ATR 递推状态）的日期必须覆盖尾部
    # 最后一根K线，否则说明行情已更新而指标缓存未重建（盘后流水线失败/
    # 进行中），混用旧锚点与新收盘价会产出错误趋势值 —— 回退全量计算。
    tail_last_date = pd.to_datetime(tail_bars["time"], errors="coerce").dropna().max()
    anchor_date = pd.to_datetime(cache_row.get("time"), errors="coerce")
    if (
        pd.notna(tail_last_date)
        and pd.notna(anchor_date)
        and pd.Timestamp(anchor_date).date() < pd.Timestamp(tail_last_date).date()
    ):
        return {"ok": False, "reason": "stale_anchor", "is_intraday": True}

    prev_volume = safe_float(volume.iloc[-1], 0.0)
    synth = build_synthetic_bar(quote, prev_volume)
    synth_close = float(synth["close"])

    fixed_atr = safe_float(cache_row.get("atr"), 0.0)
    if fixed_atr <= 0:
        return {"ok": False, "reason": "invalid_atr", "is_intraday": True}

    n_short = int(trend_config.get("n_short", 3))
    n_mid = int(trend_config.get("n_mid", 5))
    n_long = int(trend_config.get("n_long", 8))
    vol_ma_period = int(trend_config.get("vol_ma_period", 20))
    er_period = int(trend_config.get("er_period", 10))

    closes = list(close.tail(n_long)) + [synth_close]
    # vol_ma window = last (vol_ma_period-1) historical volumes + today's
    # (fixed) volume — mirrors the full-history path's rolling(20) over the
    # combined series exactly.
    volumes = list(volume.tail(vol_ma_period - 1)) + [prev_volume]

    bias_parts: list[float] = []
    slope_parts: list[float] = []
    for n, ema_col in ((n_short, "ema_s"), (n_mid, "ema_m"), (n_long, "ema_l")):
        ma_n = float(pd.Series(closes[-n:]).mean())
        bias_parts.append(safe_float((synth_close - ma_n) / fixed_atr))

        ema_prev = safe_float(cache_row.get(ema_col), 0.0)
        alpha = 2.0 / (n + 1.0)
        ema_today = alpha * synth_close + (1 - alpha) * ema_prev
        slope_parts.append(safe_float((ema_today - ema_prev) / (fixed_atr * n)))

    w_bias = [
        safe_float(trend_config.get("w_bias_short", 0.4), 0.4),
        safe_float(trend_config.get("w_bias_mid", 0.4), 0.4),
        safe_float(trend_config.get("w_bias_long", 0.2), 0.2),
    ]
    w_slope = [
        safe_float(trend_config.get("w_slope_short", 0.4), 0.4),
        safe_float(trend_config.get("w_slope_mid", 0.4), 0.4),
        safe_float(trend_config.get("w_slope_long", 0.2), 0.2),
    ]
    bias_mix = float(np.dot(w_bias, bias_parts))
    slope_mix = float(np.dot(w_slope, slope_parts))
    norm_bias = float(np.tanh(bias_mix / 2.0) * 100.0)
    norm_slope = float(np.tanh(slope_mix) * 100.0)
    price_direction = (
        safe_float(trend_config.get("w_bias_norm", 0.5), 0.5) * norm_bias
        + safe_float(trend_config.get("w_slope_norm", 0.5), 0.5) * norm_slope
    )

    vol_ma = float(pd.Series(volumes).mean()) if volumes else 0.0
    vol_ratio = (prev_volume / vol_ma) if vol_ma > 0 else 0.0
    volume_factor = 1.0 if vol_ratio >= 3.0 else max(vol_ratio / 3.0, 0.0)

    er_closes = list(close.tail(er_period)) + [synth_close]
    er_num = abs(er_closes[-1] - er_closes[0])
    er_den = sum(abs(er_closes[i] - er_closes[i - 1]) for i in range(1, len(er_closes)))
    er_now = float(np.clip(er_num / er_den, 0.0, 1.0)) if er_den > 0 else 0.0

    confidence = float(
        (volume_factor ** safe_float(trend_config.get("w_vol", 0.3), 0.3))
        * (er_now ** safe_float(trend_config.get("w_er", 0.7), 0.7))
    )
    trend_score = float(np.clip(price_direction * confidence, -100.0, 100.0))

    return {
        "ok": True,
        "reason": "ok",
        "trend_score": trend_score,
        "price_direction": price_direction,
        "confidence": confidence,
        "atr": fixed_atr,
        "price": synth_close,
        "ma_mid": float(pd.Series(closes[-n_mid:]).mean()),
        "is_intraday": True,
        "calc_details": {
            "price": synth_close,
            "ma_mid": float(pd.Series(closes[-n_mid:]).mean()),
            "atr": fixed_atr,
            "bias_short": bias_parts[0],
            "bias_mid": bias_parts[1],
            "bias_long": bias_parts[2],
            "slope_short": slope_parts[0],
            "slope_mid": slope_parts[1],
            "slope_long": slope_parts[2],
            "bias_mix": bias_mix,
            "slope_mix": slope_mix,
            "norm_bias": norm_bias,
            "norm_slope": norm_slope,
            "vol_ma": vol_ma,
            "current_volume": prev_volume,
            "vol_ratio": vol_ratio,
            "volume_factor": volume_factor,
            "er": er_now,
        },
    }

def compute_intraday_trend_score(
    historical_bars: pd.DataFrame,
    quote: dict,
    trend_config: dict,
) -> dict:
    """Calculate a trend-score snapshot for a symbol using intraday data.

    1. Compute ATR from *historical_bars only* (no synthetic bar).
    2. Use the previous day's volume as *fixed_volume*.
    3. Append the synthetic intraday bar to historical bars.
    4. Call ``calculate_trend_score_snapshot`` with ``fixed_atr``
       and ``fixed_volume`` so that today's incomplete bar does not
       contaminate ATR or volume-ratio calculations.

    Parameters
    ----------
    historical_bars: DataFrame of daily bars (no intraday bar).
        Must contain ``open, high, low, close, volume``.
    quote: real-time quote dict from TickFlow.
    trend_config: strategy configuration dict.

    Returns
    -------
    Same structure as ``calculate_trend_score_snapshot`` with an
    additional key ``is_intraday: True``.
    """
    min_bars = max(
        int(trend_config.get("n_long", 8)),
        int(trend_config.get("atr_period", 20)),
    ) + 2

    if historical_bars.empty or len(historical_bars) < min_bars - 1:
        return {
            "ok": False,
            "reason": "insufficient_historical_bars",
            "trend_score": 0.0,
            "price_direction": 0.0,
            "confidence": 0.0,
            "atr": 0.0,
            "price": _number(quote.get("price")) or 0.0,
            "ma_mid": 0.0,
            "is_intraday": True,
            "calc_details": {
                "rows": len(historical_bars),
                "required": int(min_bars),
            },
        }

    # Compute ATR from historical bars ONLY (no synthetic bar).
    atr_series = _compute_atr(historical_bars, period=int(trend_config.get("atr_period", 20)))
    fixed_atr = safe_float(atr_series.iloc[-1], default=0.0)

    # Previous day's volume.
    prev_volume = safe_float(historical_bars["volume"].iloc[-1], default=0.0)

    # Build synthetic bar.
    synth = build_synthetic_bar(quote, prev_volume)
    synth_row = pd.DataFrame([synth])

    # Concatenate: historical + synthetic bar.
    combined = pd.concat([historical_bars, synth_row], ignore_index=True)

    # Compute trend score with fixed ATR and fixed volume.
    result = calculate_trend_score_snapshot(
        combined, trend_config,
        fixed_atr=fixed_atr if fixed_atr > 0 else None,
        fixed_volume=prev_volume,
    )
    result["is_intraday"] = True
    return result

# ---------------------------------------------------------------------------
# batch intraday dashboard
# ---------------------------------------------------------------------------

# 走势图/日期窗口 DISPLAY_DAYS：与 EOD 看板同一来源（services.dashboard_common）。

def _weighted_daily_trend_series(rows_df: pd.DataFrame) -> tuple[list[str], list[float]]:
    """按日期对齐成员的趋势序列，成交额加权平均出级别每日序列。

    与 EOD 看板 ``_aggregate_daily`` 同口径：
    score(d) = Σ(score_i(d) × amount_i(d)) / Σ(amount_i(d))；
    成交额缺失（≤0 或 None）的成员按等权 1.0 兜底。
    """
    numerator: dict[str, float] = defaultdict(float)
    denominator: dict[str, float] = defaultdict(float)
    for _, row in rows_df.iterrows():
        scores = row.get("trend_score_series") or []
        dates = row.get("trend_series_dates") or []
        amounts = row.get("trend_series_amounts") or []
        for day, raw_score, raw_amount in zip(dates, scores, amounts):
            score = _number(raw_score)
            if score is None:
                continue
            weight = _number(raw_amount)
            if weight is None or weight <= 0:
                weight = 1.0
            numerator[str(day)] += score * weight
            denominator[str(day)] += weight
    days = sorted(day for day in numerator if denominator.get(day, 0.0) > 0)
    return days, [numerator[d] / denominator[d] for d in days]

def build_intraday_dashboard(
    symbols: list[str],
    db: Database,
    data_service: DataService,
    trend_config: dict,
    *,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    """Build a full intraday subject-market dashboard for *symbols*.

    Orchestrates:
    1. Batch real-time quote fetch (chunked, with progress).
    2. Per-symbol historical bar load + intraday trend-score compute.
    3. Multi-level aggregation matching the EOD dashboard structure.

    Returns a dict with the same keys as ``build_subject_dashboard_payload``
    plus ``is_intraday: True`` and ``intraday_ts``.
    """
    total = len(symbols)
    if total == 0:
        return {
            "as_of": None,
            "groups": [],
            "secondary_count": 0,
            "category_count": 0,
            "instrument_count": 0,
            "is_intraday": True,
            "intraday_ts": market_now().replace(tzinfo=None).isoformat(),
        }

    def _progress(stage: str, percent: float, message: str = "") -> None:
        if progress_callback:
            progress_callback({"stage": stage, "percent": percent, "message": message})

    _progress("quotes", 0.0, f"正在获取 {total} 个标的实时报价…")

    # --- 1. Batch quotes ---------------------------------------------------
    quotes = data_service.fetch_latest_quotes(symbols)
    quote_ok = sum(1 for q in quotes.values() if "error" not in q and q.get("price") is not None)
    _progress("quotes", 0.15, f"实时报价: {quote_ok}/{total} 成功")

    # --- 2. Load metadata + bulk cached data -------------------------------
    metadata_map = db.get_instrument_metadata_map()
    name_map: dict[str, str] = {}
    for sym, meta in metadata_map.items():
        name = str(meta.get("name", "")).strip()
        if name:
            name_map[sym] = name

    # Bulk loads (each is ONE query, replacing per-symbol full-history reads):
    # - 1y K-line tail: closes/volumes aligned with the 1y trend history
    #   (also provides the short tails for today's indicator components)
    # - latest cached indicator row per symbol: EMA/ATR recursion anchors
    # - 1y cached trend series: MA5 + phase-detection history
    from core.indicators import INDICATOR_FORMULA_VERSION
    from core.trend import TREND_FORMULA_VERSION

    tail_rows = db.load_market_tail(days=365)
    tail_frame = pd.DataFrame(tail_rows) if tail_rows else pd.DataFrame()
    if not tail_frame.empty:
        tail_frame["time"] = pd.to_datetime(tail_frame["time"], errors="coerce")
    tail_by_symbol = {
        str(sym): frame.sort_values("time").reset_index(drop=True)
        for sym, frame in tail_frame.groupby("symbol", sort=False)
    } if not tail_frame.empty else {}

    indicator_latest = db.load_indicator_latest(formula_version=INDICATOR_FORMULA_VERSION)

    trend_since = (market_now().replace(tzinfo=None) - pd.Timedelta(days=365)).date().isoformat()
    trend_by_symbol: dict[str, tuple[list, list]] = {}
    for row in db.load_trend_daily_bulk(trend_since, formula_version=TREND_FORMULA_VERSION):
        entry = trend_by_symbol.setdefault(str(row["symbol"]), ([], []))
        entry[0].append(str(row["time"])[:10])
        entry[1].append(row["trend_score"])

    # --- 3. Per-symbol computation -----------------------------------------
    instrument_rows: list[dict] = []
    failed: list[str] = []

    for idx, symbol in enumerate(symbols):
        percent = 0.15 + 0.70 * (idx + 1) / total
        _progress("compute", percent, f"计算趋势值 {idx + 1}/{total}: {symbol}")

        quote = quotes.get(symbol, {})
        if not quote or "error" in quote or quote.get("price") is None:
            failed.append(symbol)
            continue
        if not is_quote_fresh(quote):
            # 停牌/陈旧报价：其 trade date 不是今天，绝不能当今日行情用。
            failed.append(symbol)
            continue

        meta = metadata_map.get(symbol, {})
        symbol = str(symbol)

        # Cache-first path: O(1) incremental trend from cached state.
        tail = tail_by_symbol.get(symbol)
        cache_row = indicator_latest.get(symbol)
        result = compute_intraday_trend_cached(symbol, quote, tail, cache_row, trend_config) if (
            tail is not None and cache_row is not None
        ) else {"ok": False, "reason": "missing_cache", "is_intraday": True}

        hist = None
        if not result.get("ok"):
            # Fallback: full-history path (correct for uncached symbols).
            hist = db.load_market_data(symbol, price_mode="qfq")
            if hist.empty:
                failed.append(symbol)
                continue
            hist["time"] = pd.to_datetime(hist["time"], errors="coerce")
            hist = hist.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").reset_index(drop=True)
            for col in ("open", "high", "low", "close", "volume", "amount"):
                if col in hist.columns:
                    hist[col] = pd.to_numeric(hist[col], errors="coerce")
            if len(hist) < 20:
                failed.append(symbol)
                continue
            result = compute_intraday_trend_score(hist, quote, trend_config)
            if not result.get("ok"):
                failed.append(symbol)
                continue

        # --- Trend history for MA5 + phase detection ----------------------
        # Cached trend series (1y window); the intraday snapshot is appended
        # so the current phase is determined by the live score.
        hist_dates: list[str] = []
        hist_scores: list[float | None] = []
        if symbol in trend_by_symbol:
            hist_dates, hist_scores = trend_by_symbol[symbol]
        if not hist_dates:
            # Fallback: compute trend over the available history.
            if hist is None:
                hist = db.load_market_data(symbol, price_mode="qfq")
                hist["time"] = pd.to_datetime(hist["time"], errors="coerce")
                hist = hist.dropna(subset=["time", "close"]).sort_values("time").reset_index(drop=True)
            from services.market_indicators import compute_trend_indicator

            trend_result = compute_trend_indicator(hist, trend_config)
            hist_scores = trend_result.get("score", [])
            hist_dates = [pd.Timestamp(v).date().isoformat() for v in hist["time"]]

        hist_closes: list[float | None] = []
        if hist is not None and not hist.empty:
            hist_closes = [_number(v) for v in hist["close"]]
        elif symbol in tail_by_symbol and hist_dates:
            # Closes aligned to the cached trend series by date.
            close_map = {
                str(t)[:10]: _number(c)
                for t, c in zip(tail_by_symbol[symbol]["time"], tail_by_symbol[symbol]["close"])
            }
            hist_closes = [close_map.get(d) for d in hist_dates]

        intraday_score = result["trend_score"]
        intraday_price = _number(quote.get("price")) or 0.0
        extended_scores = list(hist_scores) + [intraday_score]
        extended_ma5 = _ma5(extended_scores)
        extended_closes = list(hist_closes) + [intraday_price]
        extended_dates = list(hist_dates) + [market_now().replace(tzinfo=None).date().isoformat()]

        # 级别聚合（L2/L3）的成交额权重：历史日按当日成交额（优先全量 hist，
        # 缓存路径用 1y tail），今日盘中用实时成交额；缺失日聚合时等权兜底。
        amount_src = hist if (hist is not None and not hist.empty) else tail
        amount_by_date: dict[str, float | None] = {}
        if amount_src is not None and not amount_src.empty and "amount" in amount_src.columns:
            for _t, _a in zip(amount_src["time"], amount_src["amount"]):
                amount_by_date[pd.Timestamp(_t).date().isoformat()] = _number(_a)
        extended_amounts = [amount_by_date.get(d) for d in hist_dates] + [
            _number(quote.get("amount"))
        ]

        phase_info = _detect_trend_phase(
            extended_scores, extended_ma5,
            extended_closes, extended_dates,
        )
        # Override change_pct with intraday price if phase detected.
        if phase_info["phase"] is not None and intraday_price > 0:
            # Re-lookup signal close from original hist_closes.
            sig_date = phase_info["signal_date"]
            signal_idx = None
            for i in range(len(hist_dates) - 1, -1, -1):
                if hist_dates[i] == sig_date:
                    signal_idx = i
                    break
            if signal_idx is not None and signal_idx < len(hist_closes):
                sig_close = hist_closes[signal_idx]
                if sig_close and sig_close > 0:
                    phase_info["change_pct"] = round((intraday_price / sig_close - 1.0) * 100.0, 2)

        # --- MACD 金叉/死叉相位 + 近10日K线 --------------------------------
        # 与 EOD 看板同口径（1y qfq 尾部），盘中把当日合成K线追加在末尾 —
        # 盘中金叉/死叉当天即可见；涨跌幅基准为金叉/死叉首日收盘，分子为
        # 实时价（合成K线 close = quote price）。当日K线已落库则 DB 优先。
        macd_phase_info: dict = {"phase": None, "days": None, "change_pct": None, "signal_date": None}
        kline_payload: dict = {"kline": [], "kline_ma5": []}
        macd_mini_payload: dict = {"macd_dif": [], "macd_dea": [], "macd_hist": [], "macd_dates": []}
        macd_src = hist if (hist is not None and not hist.empty) else tail
        if macd_src is not None and not macd_src.empty:
            bars = macd_src.copy()
            if not has_persisted_today_bar(bars):
                prev_vol = safe_float(bars["volume"].iloc[-1], 0.0) if "volume" in bars.columns else 0.0
                synth_bar = build_synthetic_bar(quote, prev_vol)
                quote_vol = safe_float(quote.get("volume"), 0.0)
                if quote_vol > 0:
                    synth_bar["volume"] = quote_vol
                bars = pd.concat([bars, pd.DataFrame([synth_bar])], ignore_index=True)
            macd_dates = [pd.Timestamp(v).date().isoformat() for v in bars["time"]]
            macd_phase_info = detect_macd_phase(list(bars["close"]), macd_dates)
            kline_payload = kline_mini(bars)
            macd_mini_payload = macd_mini(bars)

        name = str(meta.get("name") or name_map.get(symbol, "")).strip()
        if hist is not None and not hist.empty:
            last_volume = safe_float(hist["volume"].iloc[-1], 0.0)
        elif tail is not None and not tail.empty:
            last_volume = safe_float(tail["volume"].iloc[-1], 0.0)
        else:
            last_volume = 0.0
        instrument_rows.append(
            {
                "symbol": symbol,
                "name": name or symbol,
                "time": market_now().replace(tzinfo=None),
                "trend_score": result["trend_score"],
                # 初始化为 None：日涨幅必须由「今收/昨收」在下方重算得出，
                # 尾部数据缺失时保持 None 而不是错误地显示价格本身。
                "return_1d": None,
                "return_5d": 0.0,  # will be computed from history below
                "return_20d": 0.0,
                "return_60d": 0.0,
                "open": _number(quote.get("open")) or 0.0,
                "high": _number(quote.get("high")) or 0.0,
                "low": _number(quote.get("low")) or 0.0,
                "close": _number(quote.get("price")) or 0.0,
                "volume": last_volume,
                "amount": _number(quote.get("amount")) or 0.0,
                "category_l1": str(meta.get("category_l1") or ""),
                "category_l2": str(meta.get("category_l2") or ""),
                "category_l3": str(meta.get("category_l3") or ""),
                "priority_l1": int(meta.get("priority_l1") or 9999),
                "priority_l2": int(meta.get("priority_l2") or 9999),
                "priority_l3": int(meta.get("priority_l3") or 9999),
                "sort_order": int(meta.get("sort_order") or 999999),
                "is_intraday": True,
                "trend_phase": phase_info["phase"],
                "trend_phase_days": phase_info["days"],
                "trend_phase_change_pct": phase_info["change_pct"],
                "trend_phase_signal_date": phase_info["signal_date"],
                "macd_phase": macd_phase_info["phase"],
                "macd_phase_days": macd_phase_info["days"],
                "macd_phase_change_pct": macd_phase_info["change_pct"],
                "macd_phase_signal_date": macd_phase_info["signal_date"],
                "kline": kline_payload["kline"],
                "kline_ma5": kline_payload["kline_ma5"],
                "macd_dif": macd_mini_payload["macd_dif"],
                "macd_dea": macd_mini_payload["macd_dea"],
                "macd_hist": macd_mini_payload["macd_hist"],
                "macd_dates": macd_mini_payload["macd_dates"],
                # 聚合层（L2/L3/标的）MA5 历史的原料：含今日盘中快照的
                # 趋势序列 + 对齐日期 + 成交额权重（见 _weighted_daily_trend_series）。
                "trend_score_series": extended_scores,
                "trend_series_dates": extended_dates,
                "trend_series_amounts": extended_amounts,
            }
        )

    _progress("aggregate", 0.90, f"正在聚合 {len(instrument_rows)} 个标的…")

    # --- 4. Compute multi-period returns (from the bulk 1y tail) ------------
    for row in instrument_rows:
        frame = tail_by_symbol.get(row["symbol"])
        if frame is None or frame.empty:
            continue
        closes = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if len(closes) < 2:
            continue
        synth_close = row["close"]
        prev_close = safe_float(closes.iloc[-1], synth_close)
        if prev_close and prev_close != 0:
            row["return_1d"] = (synth_close / prev_close - 1.0) * 100.0
        for period in (5, 20, 60):
            if len(closes) > period:
                base = safe_float(closes.iloc[-(period)], prev_close)
                if base and base != 0:
                    row[f"return_{period}d"] = (synth_close / base - 1.0) * 100.0

    # --- 5. Multi-level aggregation (same structure as the EOD dashboard) ----
    source = pd.DataFrame(instrument_rows)
    if source.empty:
        _progress("done", 1.0, "无可用盘中数据")
        return {
            "as_of": market_now().replace(tzinfo=None).isoformat(),
            "groups": [],
            "secondary_count": 0,
            "category_count": 0,
            "instrument_count": 0,
            "is_intraday": True,
            "intraday_ts": market_now().replace(tzinfo=None).isoformat(),
        }

    # Filter: only instruments with full 3-level classification.
    source = source[
        (source["category_l1"].str.strip() != "")
        & (source["category_l2"].str.strip() != "")
        & (source["category_l3"].str.strip() != "")
    ].copy()

    if source.empty:
        _progress("done", 1.0, "无完整分类的标的")
        return {
            "as_of": market_now().replace(tzinfo=None).isoformat(),
            "groups": [],
            "secondary_count": 0,
            "category_count": 0,
            "instrument_count": 0,
            "is_intraday": True,
            "intraday_ts": market_now().replace(tzinfo=None).isoformat(),
        }

    # ---- helpers for aggregation ----
    def _weighted_avg_intra(rows_df: pd.DataFrame, column: str) -> float | None:
        """成交额加权平均（P2-5：与 EOD 聚合口径对齐，替代原简单平均）。

        权重规则与本模块 ``_weighted_daily_trend_series`` 相同：成交额缺失
        （≤0 或 None）的成员按等权 1.0 兜底；整列无有效值返回 None
        （mean 前 dropna，杜绝全 None 产出 NaN 进 JSON）。
        """
        if column not in rows_df:
            return None
        values = pd.to_numeric(rows_df[column], errors="coerce")
        amounts = (
            pd.to_numeric(rows_df["amount"], errors="coerce")
            if "amount" in rows_df
            else pd.Series(np.nan, index=rows_df.index)
        )
        weights = amounts.where(amounts.notna() & (amounts > 0), 1.0)
        valid = values.notna()
        if not bool(valid.any()):
            return None
        w = weights[valid].to_numpy(dtype=float)
        return float((values[valid].to_numpy(dtype=float) * w).sum() / w.sum())

    def _metrics_summary_intra(rows_df: pd.DataFrame, meta: dict, *, is_instrument: bool = False) -> dict | None:
        if rows_df.empty:
            return None
        trend_vals: list[float] = []
        for raw in rows_df["trend_score"]:
            v = _number(raw)
            if v is not None:
                trend_vals.append(v)

        if not trend_vals:
            return None

        avg_trend = _weighted_avg_intra(rows_df, "trend_score")
        avg_1d = _weighted_avg_intra(rows_df, "return_1d")
        avg_5d = _weighted_avg_intra(rows_df, "return_5d")
        avg_20d = _weighted_avg_intra(rows_df, "return_20d")
        avg_60d = _weighted_avg_intra(rows_df, "return_60d")
        total_amount = float(rows_df["amount"].sum()) if "amount" in rows_df else 0.0

        # MA5 历史与 EOD 看板同口径：级别每日趋势序列（成交额加权）→ 5日平滑；
        # trend_history 取 MA5 序列尾部（走势图/sparkline 数据源），
        # trend_ma5 取最后一个平滑值。标的级就是单成员序列，天然一致。
        series_dates, series_scores = _weighted_daily_trend_series(rows_df)
        ma5_series = _ma5(series_scores)
        latest_ma5 = ma5_series[-1] if ma5_series else None

        result = {
            "member_count": int(meta.get("member_count", len(rows_df))),
            "trend_score": avg_trend,
            "trend_ma5": latest_ma5 if latest_ma5 is not None else avg_trend,
            "daily_change_pct": avg_1d,
            "change_5d": avg_5d,
            "change_20d": avg_20d,
            "change_60d": avg_60d,
            "amount": total_amount,
            "trend_history": ma5_series[-_DISPLAY_DAYS:],
            "trend_dates": series_dates[-_DISPLAY_DAYS:],
            "as_of": market_now().replace(tzinfo=None).date().isoformat(),
            "priority_l1": _priority(meta.get("priority_l1", 9999)),
            "priority_l2": _priority(meta.get("priority_l2", 9999)),
            "priority_l3": _priority(meta.get("priority_l3", 9999)),
            "is_intraday": True,
        }

        # Pass through trend phase for instrument-level rows only.
        if is_instrument and len(rows_df) == 1:
            row = rows_df.iloc[0]
            result["trend_phase"] = row.get("trend_phase")
            result["trend_phase_days"] = _number(row.get("trend_phase_days"))
            result["trend_phase_change_pct"] = _number(row.get("trend_phase_change_pct"))
            result["trend_phase_signal_date"] = row.get("trend_phase_signal_date")
            result["macd_phase"] = row.get("macd_phase")
            result["macd_phase_days"] = _number(row.get("macd_phase_days"))
            result["macd_phase_change_pct"] = _number(row.get("macd_phase_change_pct"))
            result["macd_phase_signal_date"] = row.get("macd_phase_signal_date")
            kline = row.get("kline")
            result["kline"] = list(kline) if isinstance(kline, list) else []
            kline_ma5 = row.get("kline_ma5")
            result["kline_ma5"] = list(kline_ma5) if isinstance(kline_ma5, list) else []
            for macd_key in ("macd_dif", "macd_dea", "macd_hist", "macd_dates"):
                macd_series = row.get(macd_key)
                result[macd_key] = list(macd_series) if isinstance(macd_series, list) else []
        else:
            result["trend_phase"] = None
            result["trend_phase_days"] = None
            result["trend_phase_change_pct"] = None
            result["trend_phase_signal_date"] = None
            # 类目聚合行：K线无聚合意义，看板显示「—」；
            # MACD 相位聚合为金叉/死叉家数，在嵌套完成后按成员填充。
            result["macd_phase"] = None
            result["macd_phase_days"] = None
            result["macd_phase_change_pct"] = None
            result["macd_phase_signal_date"] = None
            result["kline"] = []
            result["kline_ma5"] = []
            result["macd_dif"] = []
            result["macd_dea"] = []
            result["macd_hist"] = []
            result["macd_dates"] = []
        # 家数字段仅类目行使用（嵌套后填充），标的行恒为 None。
        result["macd_golden_count"] = None
        result["macd_dead_count"] = None

        return result

    # L2 / L3 / instrument level aggregation.
    l2_columns = ["category_l1", "category_l2"]
    l3_columns = ["category_l1", "category_l2", "category_l3"]
    inst_columns = ["category_l1", "category_l2", "category_l3", "symbol", "name"]

    def _build_summaries(df: pd.DataFrame, group_cols: list[str], *, is_instrument: bool = False) -> list[dict]:
        summaries: list[dict] = []
        for key, grp in df.groupby(group_cols, sort=False):
            key_tuple_vals = tuple(str(v) for v in (_key_tuple(key)))
            meta_counts = {
                "member_count": grp["symbol"].nunique() if "symbol" in grp.columns else len(grp),
                "priority_l1": int(grp["priority_l1"].min()) if "priority_l1" in grp.columns else 9999,
                "priority_l2": int(grp["priority_l2"].min()) if "priority_l2" in grp.columns else 9999,
                "priority_l3": int(grp["priority_l3"].min()) if "priority_l3" in grp.columns else 9999,
            }
            summary = _metrics_summary_intra(grp, meta_counts, is_instrument=is_instrument)
            if summary:
                summary.update(dict(zip(group_cols, key_tuple_vals)))
                summaries.append(summary)
        return summaries

    l2_items = _build_summaries(source, l2_columns)
    l3_items = _build_summaries(source, l3_columns)
    instruments = _build_summaries(source, inst_columns, is_instrument=True)

    # Assign strength percentiles.
    _assign_strength(l2_items, ("category_l1",))
    _assign_strength(l3_items, ("category_l1",))
    _assign_strength(instruments, ("category_l1",))

    # Nest: instruments → l3 → l2.
    inst_by_l3: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for inst in instruments:
        inst_by_l3[(inst["category_l1"], inst["category_l2"], inst["category_l3"])].append(inst)

    def _sort(items: list[dict], name_key: str) -> None:
        items.sort(key=lambda x: (
            x["strength"] is None,
            -(x["strength"] or 0),
            x.get("priority_l3", 9999),
            str(x.get(name_key) or ""),
        ))

    for children in inst_by_l3.values():
        _sort(children, "name")

    l3_by_l2: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for l3 in l3_items:
        l3["children"] = inst_by_l3.get((l3["category_l1"], l3["category_l2"], l3["category_l3"]), [])
        l3["child_count"] = len(l3["children"])
        l3.update(_macd_counts(l3["children"]))
        l3_by_l2[(l3["category_l1"], l3["category_l2"])].append(l3)
    for children in l3_by_l2.values():
        _sort(children, "category_l3")

    l2_by_l1: dict[str, list[dict]] = defaultdict(list)
    for l2 in l2_items:
        l2["children"] = l3_by_l2.get((l2["category_l1"], l2["category_l2"]), [])
        l2["child_count"] = len(l2["children"])
        l2.update(_macd_counts([inst for l3 in l2["children"] for inst in l3["children"]]))
        l2_by_l1[l2["category_l1"]].append(l2)
    for children in l2_by_l1.values():
        _sort(children, "category_l2")

    groups = [
        {
            "category_l1": l1,
            "count": len(items),
            "items": items,
            "priority_l1": min(item.get("priority_l1", 9999) for item in items),
            "is_intraday": True,
        }
        for l1, items in l2_by_l1.items()
    ]
    groups.sort(key=lambda g: (g["priority_l1"], g["category_l1"]))

    _progress("done", 1.0, f"完成 — {len(groups)} 个一级类目, {len(instruments)} 个标的")

    return {
        "as_of": market_now().replace(tzinfo=None).isoformat(),
        "groups": groups,
        "secondary_count": len(l2_items),
        "category_count": len(l3_items),
        "instrument_count": len(instruments),
        "is_intraday": True,
        "intraday_ts": market_now().replace(tzinfo=None).isoformat(),
        # 报价失败/停牌/分类不全的标的：让调用方能区分「无数据」与「被省略」。
        "failed_symbols": sorted(set(failed)),
    }
