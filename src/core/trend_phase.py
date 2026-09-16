"""多周期趋势相位（日/周/月正负无 + 持续天数）—— canonical 实现。

相位 = 趋势值相对阈值的三态离散化（与相位迁移研究的 ``_states_from_scores``
同一口径）：

=======  ==========  ==================
相位      条件         标签
=======  ==========  ==================
正趋势     v >  +τ     ``positive``
负趋势     v <  -τ     ``negative``
无趋势     |v| <= τ    ``none``
=======  ==========  ==================

阈值按周期取（``PHASE_THRESHOLDS``）：日 ±5、周/月 ±9。日 ±5 是看板与相位
研究沿用的既有口径；周/月 ±9 来自滚动口径的分布标定
（2026-09-13 rolling-trend-calibration 研究 §6.1，已归档浓缩于
``research/README.md``：滚动周/月分布比日K 宽——σ 11.5~12.3 vs
9.0~9.1——沿用 ±5 会把「无趋势」占比从日K 的 ~2/3 压到 ~1/2，放宽到 ±9
后与日K 的三态占比语义对齐）。

持续天数 ``phase_days``：截至**最新一根有效 bar**（非 NaN），与最新状态相同
的连续 bar 数。状态当日首次转入即为 1，「无趋势」同理。计数单位与序列本身
一致——日线序列按交易日、周线序列按周、月线序列按月。

``previous_phase`` 给出**紧邻的前一根** bar 的相位，用来读「相位切换」：
``previous_phase != phase`` 即当日发生切换（此时 ``phase_days == 1``），
把两者拼起来就是「从什么相位变化到什么相位」（positive 强于 none 强于
negative，可据此判断当日走强还是走弱）。

本模块只做纯计算（序列 → 三态 + 天数）；取数（日/周/月趋势值来自
``trend_daily`` / ``trend_rolling_daily``）与看板装配在 services 层。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from core.bars import PERIOD_DAILY, PERIOD_MONTHLY, PERIOD_WEEKLY, normalize_period
from core.numfmt import number_or_none
from core.trend import safe_float

PHASE_POSITIVE = "positive"
PHASE_NEGATIVE = "negative"
PHASE_NONE = "none"

# 三态阈值（趋势值绝对值超过即判为有趋势）。
PHASE_THRESHOLDS: dict[str, float] = {
    PERIOD_DAILY: 5.0,
    PERIOD_WEEKLY: 9.0,
    PERIOD_MONTHLY: 9.0,
}

# 看板 trend_periods 的维度顺序（输出键顺序与此一致）。
PHASE_PERIODS: tuple[str, ...] = (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY)

PHASE_PERIOD_KEYS: dict[str, str] = {
    PERIOD_DAILY: "daily",
    PERIOD_WEEKLY: "weekly",
    PERIOD_MONTHLY: "monthly",
}


def phase_threshold(period: str | None) -> float:
    """周期的三态阈值：日 ±5、周/月 ±9。未知周期抛 ValueError。"""
    canonical = normalize_period(period)
    try:
        return PHASE_THRESHOLDS[canonical]
    except KeyError as exc:  # pragma: no cover - normalize_period 已限定枚举
        raise ValueError(f"no phase threshold for period {period!r}") from exc


def phase_state(value: object, threshold: float) -> str | None:
    """单值三态判定：``positive`` / ``negative`` / ``none``；非有限值返回 None。"""
    number = safe_float(value)
    if number is None or not math.isfinite(number):
        return None
    if number > threshold:
        return PHASE_POSITIVE
    if number < -threshold:
        return PHASE_NEGATIVE
    return PHASE_NONE


def _numeric_array(values) -> np.ndarray:
    """任意序列 → float64 数组（不可解析/NaN 保留为 NaN）。"""
    if values is None:
        return np.empty(0, dtype="float64")
    try:
        series = pd.to_numeric(pd.Series(values), errors="coerce")
    except (TypeError, ValueError):
        return np.empty(0, dtype="float64")
    return series.to_numpy(dtype="float64")


def phase_run(values, *, threshold: float, dates=None) -> dict:
    """最新相位、前一根 bar 的相位，以及本相位的持续 bar 数。

    从末尾最后一个有效值回扫：连续同状态的 bar 计数（转入当日 = 1）；遇到
    NaN 或状态变化即停。``dates`` 与 ``values`` 逐位对齐，用于回填
    ``phase_since``（该段相位首个 bar 的日期，取前 10 字符）。

    ``previous_phase`` 是**紧邻的前一根** bar 的相位（日线 = 前一交易日、
    周/月维度 = 上一交易日的周/月相位）：与 ``phase`` 不同即表示当日发生了
    相位切换（此时 ``phase_days`` 必为 1），相同则表示昨日同相位；前一根不
    存在或无有效值（预热期/当日为序列首根/聚合无权重日）时为 None。

    Returns:
        ``{"phase", "previous_phase", "phase_days", "phase_since"}``
        ——序列全为 NaN / 空序列时各项均为 None。
    """
    arr = _numeric_array(values)
    invalid = {
        "phase": None,
        "previous_phase": None,
        "phase_days": None,
        "phase_since": None,
    }
    if arr.size == 0:
        return invalid
    valid = np.isfinite(arr)
    if not valid.any():
        return invalid

    last = int(np.flatnonzero(valid)[-1])
    state = phase_state(float(arr[last]), threshold)
    first = last
    while (
        first - 1 >= 0
        and valid[first - 1]
        and phase_state(float(arr[first - 1]), threshold) == state
    ):
        first -= 1

    previous = None
    if last - 1 >= 0 and valid[last - 1]:
        previous = phase_state(float(arr[last - 1]), threshold)

    since = None
    if dates is not None:
        date_list = list(dates)
        if first < len(date_list) and date_list[first] is not None:
            since = str(date_list[first])[:10]
    return {
        "phase": state,
        "previous_phase": previous,
        "phase_days": last - first + 1,
        "phase_since": since,
    }


def period_state(values, period: str | None, *, dates=None) -> dict:
    """单周期的相位 bundle（看板 trend_periods 的一个维度）。

    ``values`` 为该周期的趋势值序列（日线用日K 趋势值、周线用滚动周趋势值…），
    ``trend_score`` 取末尾最后一个有效值。序列为空 / 全 NaN 时各项为 None，
    形状保持稳定（消费方无需判空）。
    """
    threshold = phase_threshold(period)
    arr = _numeric_array(values)
    valid = np.isfinite(arr)
    return {
        "trend_score": number_or_none(float(arr[valid][-1])) if valid.any() else None,
        "threshold": threshold,
        **phase_run(arr, threshold=threshold, dates=dates),
    }


def trend_periods(*, daily=None, weekly=None, monthly=None) -> dict:
    """组装看板的 ``trend_periods``：日/周/月三个维度永远同时存在。

    每个维度传入 ``period_state`` 的返回值；传 None（本调用路径没有该维度的
    数据，如新库未回填滚动表）时用空 bundle 占位——形状稳定，前端不用判空。
    """
    provided = {
        PERIOD_DAILY: daily,
        PERIOD_WEEKLY: weekly,
        PERIOD_MONTHLY: monthly,
    }
    return {
        PHASE_PERIOD_KEYS[period]: (
            provided[period]
            if provided[period] is not None
            else period_state(None, period)
        )
        for period in PHASE_PERIODS
    }
