"""看板共用件（P1-14）：EOD 看板（services/dashboard）与盘中看板
（data/intraday_service）原本逐字复制的辅助函数，单一来源。

两处实现的数值口径必须永远一致——``tests/unit/test_intraday_trend_consistency.py``
（cached vs 全量盘中双实现一致性）是这条约束的守门员。

本模块同时承载**多周期趋势相位索引**（日/周/月三态 + 持续天数）的取数与
聚合：日线维度各调用方自己就有序列（EOD 取展示帧、盘中取实时序列），
周/月维度来自 ``trend_rolling_daily`` 的长窗口物化值，故索引只产出这两个
维度，由调用方与本路径的日线 bundle 组装成 ``trend_periods``。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

from core.bars import PERIOD_MONTHLY, PERIOD_WEEKLY
from core.calendar import market_now
from core.numfmt import number_or_none
from core.trend_phase import period_state

# 走势图/日期窗口（EOD 与盘中看板一致）：约两个月的交易日数（6 周）。
DISPLAY_DAYS = 42

# 逐日强度的「最新一个点」专用排名桶：与「强度」列同口径（各标的最新可得值
# 一起排名，不受个别标的缺当日数据影响）。哨兵值不会与 ISO 日期键冲突。
_LATEST_BUCKET = "__latest__"


def ma5(values: list[float | None]) -> list[float | None]:
    series = pd.Series(values, dtype="float64").rolling(5, min_periods=5).mean()
    return [number_or_none(value) for value in series]


def strength(values: list[float], value: float | None) -> int | None:
    if value is None or not values:
        return None
    return round(sum(score <= value for score in values) * 100 / len(values))


def priority(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 999999


def key_tuple(key: object) -> tuple[object, ...]:
    return key if isinstance(key, tuple) else (key,)


def macd_counts(instruments: list[dict]) -> dict:
    """金叉/死叉家数：类目行的 MACD 相位聚合口径（成员相位计数）。"""
    golden = sum(1 for item in instruments if item.get("macd_phase") == "golden")
    dead = sum(1 for item in instruments if item.get("macd_phase") == "dead")
    return {"macd_golden_count": golden, "macd_dead_count": dead}


def assign_strength(items: list[dict], scope_columns: tuple[str, ...]) -> None:
    """同级强度百分位：trend_ma5 在 scope 内的 percentile（0-100）。"""
    values_by_scope: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for item in items:
        value = number_or_none(item.get("trend_ma5"))
        if value is not None:
            values_by_scope[tuple(str(item[column]) for column in scope_columns)].append(value)
    for item in items:
        scope = tuple(str(item[column]) for column in scope_columns)
        item["strength"] = strength(values_by_scope[scope], number_or_none(item.get("trend_ma5")))


def assign_strength_history(items: list[dict], scope_columns: tuple[str, ...]) -> None:
    """逐日强度百分位：把 ``strength_history`` 按 ``trend_dates`` 逐位挂到各 item。

    ``assign_strength`` 只算最新一天；这里对窗口内每个交易日各算一次横截面
    百分位（口径完全相同：同一 scope 内当日 MA5 的 ``<=`` 计数占比），供趋势
    mini 图悬停提示展示逐日强度。取值来自 item 自身的 ``trend_history``（MA5
    序列），与图上画出的主线、悬停展示的 MA5 永远是同一份数据。

    窗口最新一个点走 ``_LATEST_BUCKET``：与「强度」列同口径（按各标的**最新
    可得值**排名），而不是严格的当日成员。两者只在部分标的缺当日数据时才有
    差别（停牌/盘中报价失败），但此时若不对齐，悬停最新点会与行内「强度」列
    差 1~2 个百分点。
    """
    buckets: dict[tuple[str, tuple[str, ...]], dict[int, float]] = defaultdict(dict)
    for index, item in enumerate(items):
        scope = tuple(str(item[column]) for column in scope_columns)
        history = item.get("trend_history") or []
        dates = item.get("trend_dates") or []
        for offset, (date, value) in enumerate(zip(dates, history)):
            number = number_or_none(value)
            if number is not None:
                day = _LATEST_BUCKET if offset == len(history) - 1 else str(date)[:10]
                buckets[(day, scope)][index] = number
    percentiles: dict[tuple[int, str], int] = {}
    for (day, _scope), members in buckets.items():
        series = pd.Series(members)
        # rank(method="max") 即「<= 本值的成员数」，与 strength() 同一公式；
        # 逐值计数会让全市场 × 42 日的横截面退化成 O(n²)，故用排名实现。
        ranks = series.rank(method="max") / len(series) * 100
        for index, value in ranks.round().astype(int).items():
            percentiles[(index, day)] = int(value)
    for index, item in enumerate(items):
        dates = item.get("trend_dates") or []
        item["strength_history"] = [
            percentiles.get((index, _LATEST_BUCKET if offset == len(dates) - 1 else str(date)[:10]))
            for offset, date in enumerate(dates)
        ]


# ---------------------------------------------------------------------------
# 日频聚合（EOD 看板与周/月相位索引共用）
# ---------------------------------------------------------------------------

# EOD 展示帧的聚合指标：{输出列: 来源列}，均为成交额加权平均。
DASHBOARD_DAILY_METRICS: dict[str, str] = {
    "trend_score": "trend_score",
    "daily_change_pct": "return_1d",
    "change_5d": "return_5d",
    "change_20d": "return_20d",
    "change_60d": "return_60d",
    # E-BIAS（均线偏离度）：类目级按成交额加权，与 trend_score 同口径。
    # 单位与 daily_change_pct 一致为百分比（缓存列 e_bias20 是 decimal，
    # 在 build_subject_dashboard_payload 里 ×100 转换）。
    "e_bias_pct": "e_bias_pct",
    "close": "close",
}


def aggregate_daily(
    frame: pd.DataFrame,
    group_columns: list[str],
    metrics: dict[str, str] | None = None,
) -> pd.DataFrame:
    """按 ``group_columns`` + 交易日聚合 ``frame``，逐指标做成交额加权平均。

    权重规则：成交额缺失或 ≤0 的成员当日**不计入**该指标的分子/分母
    （``amount`` 总列仍按 ≥0 求和，供热力图面积使用）。
    """
    metrics = DASHBOARD_DAILY_METRICS if metrics is None else metrics
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


# ---------------------------------------------------------------------------
# 多周期趋势相位（周/月维度）
# ---------------------------------------------------------------------------

# 周/月相位的回扫窗口（自然日）：滚动月状态的最长持续段实测约 296 个交易日
# （33 年全历史），窗口取 ~410 个交易日留足余量；表内没有更早的行时按窗口内
# 计数（phase_since 会落在窗口首日，消费方可据此识别截断）。
PHASE_LOOKBACK_DAYS = 600

_PHASE_INDEX_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("l2", ("category_l1", "category_l2")),
    ("l3", ("category_l1", "category_l2", "category_l3")),
)


def load_period_trend_frame(
    db,
    symbols,
    *,
    lookback_days: int = PHASE_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """长窗口周/月趋势值 + 计权成交额（symbol/time/weekly/monthly/amount）。

    周/月趋势值取自 ``trend_rolling_daily``（qfq 日K 派生的滚动锚定口径，
    ``core.rolling_bars``），权重为 qfq 日K 成交额——与看板其余聚合指标同一
    把尺子。该表是**派生表**：新库尚未回填时返回空表，上层的周/月相位即为
    空 bundle（trend_score/phase/phase_days 均为 null）。
    """
    empty = pd.DataFrame(columns=["symbol", "time", "weekly", "monthly", "amount"])
    unique = [symbol for symbol in dict.fromkeys(str(s or "").strip().upper() for s in symbols or []) if symbol]
    if not unique:
        return empty

    start = (market_now().date() - timedelta(days=max(int(lookback_days), 1))).isoformat()
    rolling = db.load_rolling_trend_many(unique, start=start)
    frames = []
    for symbol, frame in (rolling or {}).items():
        if frame is None or frame.empty:
            continue
        part = frame.rename(columns={"w_trend": "weekly", "m_trend": "monthly"})
        frames.append(part[["time", "weekly", "monthly"]].assign(symbol=str(symbol)))
    if not frames:
        return empty

    trend = pd.concat(frames, ignore_index=True)
    trend["time"] = pd.to_datetime(trend["time"], errors="coerce")
    trend = trend.dropna(subset=["time"])

    tail = db.load_market_tail(days=max(int(lookback_days), 1))
    if tail:
        amounts = pd.DataFrame(tail)[["symbol", "time", "amount"]]
        amounts["time"] = pd.to_datetime(amounts["time"], errors="coerce")
        trend = trend.merge(amounts, on=["symbol", "time"], how="left")
    else:
        trend["amount"] = np.nan
    return trend


def build_period_phase_index(
    db,
    symbol_categories: dict[str, tuple[str, str, str]],
    *,
    lookback_days: int = PHASE_LOOKBACK_DAYS,
) -> dict[str, dict]:
    """周/月相位索引：``{"instruments"|"l3"|"l2": {键: {"weekly","monthly"}}}``。

    - 标的层：直接用该标的自己的滚动序列（单成员，无需聚合）；
    - 类目层（L2/L3）：把成员的周/月趋势值按交易日做**成交额加权**聚合
      （``aggregate_daily``，与类目 trend_score 同口径），再对聚合序列求相位。

    ``symbol_categories`` 为 ``{symbol: (category_l1, category_l2, category_l3)}``。
    键一律是**元组**：标的层为 ``(symbol,)``，L2 为 ``(l1, l2)``，L3 为
    ``(l1, l2, l3)``——调用方按本层分组列的对应子集拼键即可直接命中。
    日线维度不在本索引内：各调用方自己就有日线序列（EOD 用展示帧、盘中用实时
    序列），由调用方与本索引的周/月 bundle 一起组装成 ``trend_periods``。
    """
    index: dict[str, dict] = {"instruments": {}, "l3": {}, "l2": {}}
    symbols = [
        symbol
        for symbol, path in (symbol_categories or {}).items()
        if symbol and all(str(level or "").strip() for level in path)
    ]
    frame = load_period_trend_frame(db, symbols, lookback_days=lookback_days)
    if frame.empty:
        return index

    paths = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "category_l1": str(symbol_categories[symbol][0]),
                "category_l2": str(symbol_categories[symbol][1]),
                "category_l3": str(symbol_categories[symbol][2]),
            }
            for symbol in symbols
        ]
    )
    frame = frame[frame["symbol"].isin(set(symbols))].merge(paths, on="symbol", how="inner")
    if frame.empty:
        return index

    numeric = {"weekly": "weekly", "monthly": "monthly"}
    for level, key_columns in _PHASE_INDEX_LEVELS:
        aggregated = aggregate_daily(frame, list(key_columns), metrics=numeric)
        for raw_key, group in aggregated.groupby(list(key_columns), sort=False):
            group = group.sort_values("time")
            dates = [str(value)[:10] for value in group["time"]]
            key = tuple(str(value) for value in (raw_key if isinstance(raw_key, tuple) else (raw_key,)))
            index[level][key] = {
                "weekly": period_state(group["weekly"], PERIOD_WEEKLY, dates=dates),
                "monthly": period_state(group["monthly"], PERIOD_MONTHLY, dates=dates),
            }

    for symbol, group in frame.groupby("symbol", sort=False):
        group = group.sort_values("time")
        dates = [str(value)[:10] for value in group["time"]]
        index["instruments"][(str(symbol),)] = {
            "weekly": period_state(group["weekly"], PERIOD_WEEKLY, dates=dates),
            "monthly": period_state(group["monthly"], PERIOD_MONTHLY, dates=dates),
        }
    return index
