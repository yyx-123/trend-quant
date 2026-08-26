"""Dashboard aggregation service — EOD subject dashboard payload builder
plus the shared revision cache.

All computation formerly lived in app.routers.subject_market; the router
now only orchestrates HTTP. Intraday overlay is layered on read paths (P1.4)
via data.indicator_store, not here.
"""

from __future__ import annotations

import threading
from collections import defaultdict

import numpy as np
import pandas as pd

from audit.app_logger import get_logger
from core.indicators import detect_macd_phase, kline_mini, macd_mini
from core.trend import TREND_FORMULA_VERSION, _detect_trend_phase
from data.indicator_store import get_series
from data.storage.db import get_db

logger = get_logger(__name__)

# 看板共用件（EOD/盘中同一来源，P1-14；盘中侧在 data/intraday_service）
from core.numfmt import number_or_none as _number
from services.dashboard_common import (
    DISPLAY_DAYS,
)
from services.dashboard_common import (
    assign_strength as _assign_strength,
)
from services.dashboard_common import (
    key_tuple as _key_tuple,
)
from services.dashboard_common import (
    ma5 as _ma5,
)
from services.dashboard_common import (
    macd_counts as _macd_counts,
)
from services.dashboard_common import (
    priority as _priority,
)

SOURCE_HISTORY_DAYS = 90
# MACD 相位/K线 mini 图的历史窗口：与盘中看板 (intraday_service) 同口径，
# EMA26/DEA9 需要足够预热长度，且长趋势的金叉日可能远在 90 日窗口之前。
MACD_HISTORY_DAYS = 365


class RevisionCache:
    """get-or-recompute cache keyed by a data revision tuple."""

    def __init__(self) -> None:
        self._cached: tuple[tuple, dict] | None = None
        # Serializes cold rebuilds across the request threadpool and the
        # background warm thread: one caller computes, the rest wait and then
        # read the fresh payload instead of duplicating a multi-second build.
        self._lock = threading.Lock()

    def get_or_compute(self, revision: tuple, compute) -> dict:
        if self._cached is not None and self._cached[0] == revision:
            return self._cached[1]
        with self._lock:
            if self._cached is not None and self._cached[0] == revision:
                return self._cached[1]
            payload = compute()
            self._cached = (revision, payload)
            return payload


# 模块级单例（P2-10）：Web（routers/subject_market）与 MCP（trend_mcp/server）
# 共用同一份缓存——此前两边各 new 一个，同一份秒级全市场计算被缓存两份，
# 冷启动/数据更新后双倍开销。
dashboard_revision_cache = RevisionCache()


def _aggregate_daily(frame: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    metrics = {
        "trend_score": "trend_score",
        "daily_change_pct": "return_1d",
        "change_5d": "return_5d",
        "change_20d": "return_20d",
        "change_60d": "return_60d",
        "close": "close",
    }
    columns = [*group_columns, "time", "amount", *metrics.values()]
    work = frame[columns].copy()
    amount = pd.to_numeric(work["amount"], errors="coerce").to_numpy(dtype=float)
    valid_amount = np.isfinite(amount) & (amount > 0)
    work["_amount_total"] = np.where(np.isfinite(amount) & (amount >= 0), amount, 0.0)
    aggregation_columns = ["_amount_total"]
    for target, source in metrics.items():
        values = pd.to_numeric(work[source], errors="coerce").to_numpy(dtype=float)
        valid = valid_amount & np.isfinite(values)
        numerator = f"_{target}_numerator"
        denominator = f"_{target}_denominator"
        work[numerator] = np.where(valid, values * amount, 0.0)
        work[denominator] = np.where(valid, amount, 0.0)
        aggregation_columns.extend([numerator, denominator])
    daily = work.groupby([*group_columns, "time"], as_index=False, sort=True)[aggregation_columns].sum()
    daily["amount"] = daily.pop("_amount_total").where(lambda values: values > 0, np.nan)
    for target in metrics:
        numerator = daily.pop(f"_{target}_numerator")
        denominator = daily.pop(f"_{target}_denominator")
        daily[target] = (numerator / denominator.replace(0.0, np.nan)).astype("float64")
    return daily


def _metrics_summary(daily: pd.DataFrame, metadata: dict) -> dict | None:
    if daily.empty:
        return None
    daily = daily.sort_values("time")
    raw_trend = [_number(value) for value in daily["trend_score"]]
    trend_ma5 = _ma5(raw_trend)
    raw_close = [_number(value) for value in daily["close"]] if "close" in daily.columns else []
    dates = [pd.Timestamp(value).date().isoformat() for value in daily["time"]]
    recent = daily.tail(DISPLAY_DAYS)
    latest = daily.iloc[-1]

    phase_info = _detect_trend_phase(raw_trend, trend_ma5, raw_close, dates)

    return {
        "member_count": int(metadata["member_count"]),
        "trend_score": _number(latest["trend_score"]),
        "trend_ma5": trend_ma5[-1] if trend_ma5 else None,
        "daily_change_pct": _number(latest["daily_change_pct"]),
        "change_5d": _number(latest["change_5d"]),
        "change_20d": _number(latest["change_20d"]),
        "change_60d": _number(latest["change_60d"]),
        "amount": _number(latest["amount"]),
        # 近20日平均成交额：热力图框面积依据，比单日成交额更稳定。
        "amount_avg20": _number(daily["amount"].tail(20).mean()),
        "trend_history": trend_ma5[-DISPLAY_DAYS:],
        "trend_dates": [pd.Timestamp(value).date().isoformat() for value in recent["time"]],
        "as_of": pd.Timestamp(latest["time"]).date().isoformat(),
        "priority_l1": _priority(metadata["priority_l1"]),
        "priority_l2": _priority(metadata["priority_l2"]),
        "priority_l3": _priority(metadata["priority_l3"]),
        "trend_phase": phase_info["phase"],
        "trend_phase_days": phase_info["days"],
        "trend_phase_change_pct": phase_info["change_pct"],
        "trend_phase_signal_date": phase_info["signal_date"],
    }


def _build_level_summaries(calculated: pd.DataFrame, group_columns: list[str]) -> list[dict]:
    metadata_frame = (
        calculated.groupby(group_columns, as_index=False, sort=False)
        .agg(
            member_count=("symbol", "nunique"),
            priority_l1=("priority_l1", "min"),
            priority_l2=("priority_l2", "min"),
            priority_l3=("priority_l3", "min"),
        )
    )
    metadata_by_key = {
        tuple(str(row._asdict()[column]) for column in group_columns): row._asdict()
        for row in metadata_frame.itertuples(index=False)
    }
    daily = _aggregate_daily(calculated, group_columns)
    summaries: list[dict] = []
    for raw_key, level_daily in daily.groupby(group_columns, sort=False):
        key = tuple(str(value) for value in _key_tuple(raw_key))
        summary = _metrics_summary(level_daily, metadata_by_key[key])
        if summary is not None:
            summary.update(dict(zip(group_columns, key)))
            summaries.append(summary)
    return summaries


def _assign_envelope(item: dict, components: list[dict]) -> None:
    """Attach MA5 extrema of ``components`` to ``item``, aligned by trading date."""
    values_by_date: dict[str, list[float]] = defaultdict(list)
    for component in components:
        for date, value in zip(component.get("trend_dates", []), component.get("trend_history", [])):
            number = _number(value)
            if number is not None:
                values_by_date[str(date)].append(number)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for date in item.get("trend_dates", []):
        values = values_by_date.get(str(date), [])
        upper.append(max(values) if values else None)
        lower.append(min(values) if values else None)
    item["trend_upper_history"] = upper
    item["trend_lower_history"] = lower


def _empty_macd_kline() -> dict:
    """类目聚合行的 MACD/K线占位：K线无聚合意义，看板显示「—」。"""
    return {
        "macd_phase": None,
        "macd_phase_days": None,
        "macd_phase_change_pct": None,
        "macd_phase_signal_date": None,
        "macd_golden_count": None,
        "macd_dead_count": None,
        "kline": [],
        "kline_ma5": [],
        "macd_dif": [],
        "macd_dea": [],
        "macd_hist": [],
        "macd_dates": [],
    }


def _macd_kline_payloads(db) -> dict[str, dict]:
    """Per-symbol MACD cross phase + 10-day kline mini from the 1y qfq tail.

    仅具体标的级使用。MACD 用与盘中看板同口径的 1 年尾部收盘价计算
    （warmup=True，与 indicator_daily 缓存的 macd_dif/dea 一致）。
    """
    rows = db.load_market_tail(days=MACD_HISTORY_DAYS)
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame = frame.dropna(subset=["time", "open", "high", "low", "close"])
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    payloads: dict[str, dict] = {}
    for symbol, history in frame.groupby("symbol", sort=False):
        history = history.sort_values("time").reset_index(drop=True)
        dates = [pd.Timestamp(value).date().isoformat() for value in history["time"]]
        phase = detect_macd_phase(list(history["close"]), dates)
        payloads[str(symbol)] = {
            "macd_phase": phase["phase"],
            "macd_phase_days": phase["days"],
            "macd_phase_change_pct": phase["change_pct"],
            "macd_phase_signal_date": phase["signal_date"],
            # 家数字段仅类目行使用，标的行恒为 None。
            "macd_golden_count": None,
            "macd_dead_count": None,
            **kline_mini(history),
            **macd_mini(history),
        }
    return payloads


def _sort_items(items: list[dict], name_key: str) -> None:
    items.sort(
        key=lambda item: (
            item["strength"] is None,
            -(item["strength"] or 0),
            item["priority_l3"],
            str(item.get(name_key) or ""),
        )
    )


def build_subject_dashboard_payload(db=None) -> dict:
    db = db or get_db()
    rows = db.load_market_dashboard_history(days=SOURCE_HISTORY_DAYS)
    if not rows:
        return {
            "as_of": None,
            "groups": [],
            "secondary_count": 0,
            "category_count": 0,
            "instrument_count": 0,
        }

    source = pd.DataFrame(rows)
    if "name" not in source.columns:
        source["name"] = source["symbol"]
    source["name"] = source["name"].fillna(source["symbol"]).astype(str)
    source["time"] = pd.to_datetime(source["time"], errors="coerce")
    source = source.dropna(subset=["time", "open", "high", "low", "close"]).copy()
    for column in ("open", "high", "low", "close", "volume", "amount"):
        source[column] = pd.to_numeric(source[column], errors="coerce")

    instrument_frames: list[pd.DataFrame] = []
    since = (pd.Timestamp.now() - pd.Timedelta(days=SOURCE_HISTORY_DAYS * 2)).date().isoformat()
    # One bulk query for all cached trend scores (version-filtered). A symbol
    # uses the bulk lookup only when its cached rows cover its latest market
    # date; otherwise it falls back to get_series (freshness check + live
    # compute) — stale caches never silently produce NULLs (kimi review §3.4).
    trend_lookup: dict[tuple[str, str], float | None] = {}
    trend_last: dict[str, str] = {}
    if db.get_param_set("default") is not None:
        for row in db.load_trend_daily_bulk(since, formula_version=TREND_FORMULA_VERSION):
            key = (row["symbol"], str(row["time"])[:10])
            trend_lookup[key] = row["trend_score"]
            if row["symbol"] not in trend_last or str(row["time"]) > trend_last[row["symbol"]]:
                trend_last[row["symbol"]] = str(row["time"])
    market_last_by_symbol = source.groupby("symbol")["time"].max()
    for symbol, history in source.groupby("symbol", sort=False):
        data = history.sort_values("time").reset_index(drop=True).copy()
        symbol = str(symbol)
        market_last = str(market_last_by_symbol.get(symbol, ""))
        if symbol in trend_last and market_last and trend_last[symbol] >= market_last:
            data["trend_score"] = [
                trend_lookup.get((symbol, str(t)[:10]), np.nan) for t in data["time"]
            ]
        else:
            # Fallback path: one store call per symbol, then per-date lookups
            # (previously get_series was called per day — 61x redundant work).
            trend_series = get_series(symbol, "trend_score", db=db, since=since)
            data["trend_score"] = [
                trend_series.get(pd.Timestamp(t), np.nan) for t in data["time"]
            ]
        for period in (1, 5, 20, 60):
            data[f"return_{period}d"] = data["close"].pct_change(periods=period) * 100.0
        instrument_frames.append(data)
    calculated = pd.concat(instrument_frames, ignore_index=True)

    l2_columns = ["category_l1", "category_l2"]
    l3_columns = [*l2_columns, "category_l3"]
    instrument_columns = [*l3_columns, "symbol", "name"]
    l2_items = _build_level_summaries(calculated, l2_columns)
    l3_items = _build_level_summaries(calculated, l3_columns)
    instruments = _build_level_summaries(calculated, instrument_columns)

    # MACD 金叉/死叉相位 + 近10日K线 mini 图：仅具体标的级；类目聚合行的
    # 聚合口径（成交额加权 MACD 并无意义）待定义，先给占位（看板显示 —）。
    macd_payloads = _macd_kline_payloads(db)
    for item in instruments:
        item.update(macd_payloads.get(str(item["symbol"])) or _empty_macd_kline())
    for item in (*l2_items, *l3_items):
        item.update(_empty_macd_kline())

    _assign_strength(l2_items, ("category_l1",))
    _assign_strength(l3_items, ("category_l1",))
    _assign_strength(instruments, ("category_l1",))

    instruments_by_l3: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for instrument in instruments:
        instruments_by_l3[(instrument["category_l1"], instrument["category_l2"], instrument["category_l3"])].append(instrument)
    for children in instruments_by_l3.values():
        _sort_items(children, "name")

    l3_by_l2: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for l3 in l3_items:
        l3["children"] = instruments_by_l3[(l3["category_l1"], l3["category_l2"], l3["category_l3"])]
        l3["child_count"] = len(l3["children"])
        l3.update(_macd_counts(l3["children"]))
        _assign_envelope(l3, l3["children"])
        l3_by_l2[(l3["category_l1"], l3["category_l2"])].append(l3)
    for children in l3_by_l2.values():
        _sort_items(children, "category_l3")

    l2_by_l1: dict[str, list[dict]] = defaultdict(list)
    for l2 in l2_items:
        l2["children"] = l3_by_l2[(l2["category_l1"], l2["category_l2"])]
        l2["child_count"] = len(l2["children"])
        l2.update(_macd_counts([inst for l3 in l2["children"] for inst in l3["children"]]))
        _assign_envelope(l2, l2["children"])
        l2_by_l1[l2["category_l1"]].append(l2)
    for children in l2_by_l1.values():
        _sort_items(children, "category_l2")

    groups = [
        {
            "category_l1": l1,
            "count": len(items),
            "items": items,
            "priority_l1": min(item["priority_l1"] for item in items),
        }
        for l1, items in l2_by_l1.items()
    ]
    groups.sort(key=lambda group: (group["priority_l1"], group["category_l1"]))
    return {
        "as_of": max((item["as_of"] for item in l2_items), default=None),
        "groups": groups,
        "secondary_count": len(l2_items),
        "category_count": len(l3_items),
        "instrument_count": int(source["symbol"].nunique()),
    }
