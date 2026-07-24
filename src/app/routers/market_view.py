from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.instrument_display import format_symbol_display, load_instrument_name_map, strip_etf_suffix
from core.calendar import is_past_market_open, previous_trading_day
from core.symbols import normalize_symbol
from data.intraday_service import compute_intraday_trend_score
from data.service import DataService
from data.storage.db import get_db
from core.trend import safe_float

from services.market_indicators import (
    ATR_PERIODS,
    BIAS_PERIODS,
    DEFAULT_RSI_PERIOD,
    MA_PERIODS,
    TREND_MA_PERIODS,
    VOL_MA_PERIODS,
    compute_market_indicators,
    trend_config as _trend_config,
)

router = APIRouter(prefix="/market-view", tags=["market-view"])
templates = Jinja2Templates(directory="web/templates")
logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20000
MAX_LIMIT = 50000


def _normalize_symbol(raw_symbol: str) -> str:
    return normalize_symbol(raw_symbol)


def _config_name_map() -> dict[str, str]:
    return load_instrument_name_map()


def _category_path(meta: dict | None) -> str:
    if not meta:
        return ""
    parts = [
        str(meta.get("category_l1") or "").strip(),
        str(meta.get("category_l2") or "").strip(),
        str(meta.get("category_l3") or "").strip(),
    ]
    return "-".join(part for part in parts if part)


def _display_with_category(display_name: str, meta: dict | None) -> str:
    path = _category_path(meta)
    return f"{display_name}（{path}）" if path else display_name


def _metadata_sort_key(meta: dict | None, symbol: str) -> tuple:
    if not meta:
        return (1, 9999, 9999, 9999, 999999, symbol)
    return (
        0,
        int(meta.get("priority_l1") or 9999),
        int(meta.get("priority_l2") or 9999),
        int(meta.get("priority_l3") or 9999),
        int(meta.get("sort_order") or 999999),
        symbol,
    )


def _market_symbol_item(symbol: str, name_map: dict[str, str], metadata: dict | None) -> dict:
    name = str((metadata or {}).get("name") or name_map.get(symbol, ""))
    display_name = format_symbol_display(symbol, name)
    display_label = _display_with_category(display_name, metadata)
    category_path = _category_path(metadata)
    factor_tags = list((metadata or {}).get("factor_tags") or [])
    return {
        "symbol": symbol,
        "name": name,
        "display_name": display_name,
        "display_label": display_label,
        "category_l1": str((metadata or {}).get("category_l1") or ""),
        "category_l2": str((metadata or {}).get("category_l2") or ""),
        "category_l3": str((metadata or {}).get("category_l3") or ""),
        "category_path": category_path,
        "factor_tags": factor_tags,
        "sort_order": int((metadata or {}).get("sort_order") or 999999),
    }


def _num(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pd.isna(n):
        return None
    return round(n, 6)


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _series(values: Iterable[object]) -> list[float | None]:
    return [_num(v) for v in values]


def _date_only(value: object) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.date().isoformat()


def _validate_trend_config(cfg: dict) -> None:
    n_short = int(cfg.get("n_short", 5))
    n_mid = int(cfg.get("n_mid", 10))
    n_long = int(cfg.get("n_long", 20))
    atr_period = int(cfg.get("atr_period", 20))
    if min(n_short, n_mid, n_long, atr_period) <= 0:
        raise HTTPException(status_code=400, detail="趋势值参数必须为正整数")
    if not (n_short < n_mid < n_long):
        raise HTTPException(status_code=400, detail="要求趋势值参数 n_short < n_mid < n_long")


def build_market_payload(
    symbol: str,
    df: pd.DataFrame,
    name: str = "",
    metadata: dict | None = None,
    trend_cfg: dict | None = None,
    rsi_period: int = DEFAULT_RSI_PERIOD,
) -> dict:
    display_name = format_symbol_display(symbol, name)
    display_label = _display_with_category(display_name, metadata)
    meta_payload = {
        "category_l1": str((metadata or {}).get("category_l1") or ""),
        "category_l2": str((metadata or {}).get("category_l2") or ""),
        "category_l3": str((metadata or {}).get("category_l3") or ""),
        "category_path": _category_path(metadata),
        "factor_tags": list((metadata or {}).get("factor_tags") or []),
        "region_tag": str((metadata or {}).get("region_tag") or ""),
    }
    if df.empty:
        return {
            "symbol": symbol,
            "name": name,
            "display_name": display_name,
            "display_label": display_label,
            "dates": [],
            "candles": [],
            "volumes": [],
            "amounts": [],
            "indicators": {},
            "meta": {"rows": 0, "start": None, "end": None, **meta_payload},
        }

    data = df.copy()
    data["time"] = pd.to_datetime(data["time"], errors="coerce")
    data = data.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")

    dates = [_date_only(v) for v in data["time"]]
    candles = [
        [_num(row.open), _num(row.close), _num(row.low), _num(row.high)]
        for row in data.itertuples(index=False)
    ]
    volumes = _series(data.get("volume", pd.Series(index=data.index)))
    amounts = _series(data.get("amount", pd.Series(index=data.index)))
    indicators = compute_market_indicators(data, trend_cfg, rsi_period)

    return {
        "symbol": symbol,
        "name": name,
        "display_name": display_name,
        "display_label": display_label,
        "dates": dates,
        "candles": candles,
        "volumes": volumes,
        "amounts": amounts,
        "indicators": indicators,
        "meta": {
            "rows": int(len(data)),
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            "ma_periods": list(MA_PERIODS),
            "atr_periods": list(ATR_PERIODS),
            "bias_periods": list(BIAS_PERIODS),
            "volume_ma_periods": list(VOL_MA_PERIODS),
            "trend_config": indicators.get("trend", {}).get("config", {}),
            "rsi_config": {"period": int(indicators.get("rsi", {}).get("period") or rsi_period)},
            **meta_payload,
        },
    }


@router.get("", response_class=HTMLResponse)
async def market_view_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="market_view.html",
        request=request,
        context={"title": "标的查看"},
    )


@router.get("/api/symbols")
async def list_market_symbols() -> dict:
    db = get_db()
    name_map = _config_name_map()
    metadata_by_symbol = db.get_instrument_metadata_map()
    symbols = db.list_market_symbols()
    items = [
        _market_symbol_item(symbol, name_map, metadata_by_symbol.get(symbol))
        for symbol in symbols
    ]
    items.sort(key=lambda item: _metadata_sort_key(metadata_by_symbol.get(item["symbol"]), item["symbol"]))
    return {"items": items, "count": len(items)}


@router.get("/api/daily")
async def get_market_daily(
    symbol: str = Query(..., min_length=1),
    start_date: str = "",
    end_date: str = "",
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    trend_n_short: int | None = Query(default=None, ge=1, le=300),
    trend_n_mid: int | None = Query(default=None, ge=1, le=300),
    trend_n_long: int | None = Query(default=None, ge=1, le=500),
    trend_atr_period: int | None = Query(default=None, ge=1, le=300),
    rsi_period: int = Query(default=DEFAULT_RSI_PERIOD, ge=2, le=300),
    intraday: bool = Query(default=False),
) -> dict:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="标的无效")

    db = get_db()
    df = db.load_market_data(normalized_symbol)
    if df.empty:
        raise HTTPException(status_code=404, detail="未找到本地日 K 数据")

    data = df.copy()
    data["time"] = pd.to_datetime(data["time"], errors="coerce")
    data = data.dropna(subset=["time"]).sort_values("time")

    if end_date.strip():
        end_ts = pd.to_datetime(end_date, errors="coerce")
    else:
        end_ts = data["time"].max()
    if pd.isna(end_ts):
        raise HTTPException(status_code=404, detail="日 K 日期无效")

    if start_date.strip():
        start_ts = pd.to_datetime(start_date, errors="coerce")
        if pd.isna(start_ts):
            raise HTTPException(status_code=400, detail="开始日期格式应为 YYYY-MM-DD")
    else:
        start_ts = data["time"].min()

    if start_ts > end_ts:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    data = data[(data["time"] >= start_ts) & (data["time"] <= end_ts)]
    if len(data) > limit:
        data = data.tail(limit)

    metadata = db.get_instrument_metadata(normalized_symbol) if hasattr(db, "get_instrument_metadata") else None
    name = str((metadata or {}).get("name") or _config_name_map().get(normalized_symbol, ""))
    trend_overrides = {
        key: value
        for key, value in {
            "n_short": _optional_int(trend_n_short),
            "n_mid": _optional_int(trend_n_mid),
            "n_long": _optional_int(trend_n_long),
            "atr_period": _optional_int(trend_atr_period),
        }.items()
        if value is not None
    }
    trend_cfg = _trend_config(trend_overrides)
    _validate_trend_config(trend_cfg)
    rsi_period_value = _optional_int(rsi_period) or DEFAULT_RSI_PERIOD
    payload = build_market_payload(normalized_symbol, data, name, metadata, trend_cfg, rsi_period_value)
    payload["meta"]["requested_start"] = _date_only(start_ts)
    payload["meta"]["requested_end"] = _date_only(end_ts)
    payload["meta"]["limit"] = int(limit)
    payload["meta"]["is_intraday"] = False

    # --- Intraday overlay -------------------------------------------------
    # Gate on is_past_market_open: on a trading day at/past the 9:30 open,
    # today's bar must be present. If the daily write job (default 16:30)
    # has already persisted it, the DB data is used as-is; otherwise a
    # synthetic bar is built from live quotes — this also covers the
    # post-close window before the write job runs.
    if intraday and is_past_market_open() and (not end_date.strip() or end_ts.date() >= datetime.now().date()):
        try:
            hist = db.load_market_data(normalized_symbol, price_mode="qfq")
            if not hist.empty:
                hist["time"] = pd.to_datetime(hist["time"], errors="coerce")
                hist = hist.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").reset_index(drop=True)
                for col in ("open", "high", "low", "close", "volume", "amount"):
                    if col in hist.columns:
                        hist[col] = pd.to_numeric(hist[col], errors="coerce")

            if not hist.empty and hist["time"].iloc[-1].date() >= datetime.now().date():
                # Today's bar is already persisted — nothing to overlay.
                pass
            elif not hist.empty:
                data_service = DataService()
                quote = data_service.fetch_latest_quote(normalized_symbol)
                if quote and quote.get("price") is not None:
                    intraday_result = compute_intraday_trend_score(hist, quote, trend_cfg)
                    if intraday_result.get("ok"):
                        # Append the intraday synthetic candle to the chart data.
                        from data.intraday_service import build_synthetic_bar
                        prev_vol = safe_float(hist["volume"].iloc[-1], 0.0) if len(hist) > 0 else 0.0
                        synth = build_synthetic_bar(quote, prev_vol)
                        # Prefer the quote's own volume (post-close it is the
                        # full-day actual); fall back to the previous-day
                        # approximation while the session is still running.
                        quote_vol = safe_float(quote.get("volume"), 0.0)
                        if quote_vol > 0:
                            synth["volume"] = quote_vol
                        synth_time = datetime.now()
                        payload["dates"].append(_date_only(synth_time))
                        payload["candles"].append([
                            _num(synth["open"]),
                            _num(synth["close"]),
                            _num(synth["low"]),
                            _num(synth["high"]),
                        ])
                        payload["volumes"].append(_num(synth["volume"]))
                        payload["amounts"].append(_num(synth["amount"]))

                        # Add intraday trend score snapshot.
                        payload["indicators"]["trend_intraday"] = {
                            "score": intraday_result["trend_score"],
                            "price_direction": intraday_result["price_direction"],
                            "confidence": intraday_result["confidence"],
                            "atr": intraday_result["atr"],
                            "price": intraday_result["price"],
                            "ma_mid": intraday_result["ma_mid"],
                            "calc_details": intraday_result.get("calc_details", {}),
                        }
                        payload["meta"]["is_intraday"] = True
                        payload["meta"]["intraday_ts"] = datetime.now().isoformat()
        except Exception as exc:
            # Fall back to EOD data if intraday fetch fails.
            logger.warning("Intraday overlay failed for %s; falling back to EOD: %s", normalized_symbol, exc)

    return payload
