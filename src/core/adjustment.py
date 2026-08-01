"""除权因子 → 本地等比前复权（qfq）物化。

设计背景：vendor 直接下发的 qfq 历史会在每次除权后被回溯改写（日更增量拼接
因此需要昂贵的断裂检测 + 整标重拉），且算法口径不可控（本项目曾误用等差前复权
forward_additive 导致早期高分红标的出现负价格）。改为「raw 行情为唯一真源 +
除权因子本地物化 qfq」后：

- raw（不复权）历史行永不回溯改写，日更只需 append；
- 除权 = 因子表多一行 → 本地重算 qfq，无需重拉任何行情；
- 等比乘法数学上不可能产生负价格。

因子语义（已对 TickFlow vendor ``forward`` 全量历史逐行零误差验证）：

- 因子表每个除权日一条 ``ex_factor``（> 1，分红/送转的复合调整系数）；
- ``qfq(t) = raw(t) / Π_{ex_date_i >= t} f_i`` —— 除权日**当日**的 bar 也参与除权；
- ``volume`` / ``amount`` 不调整（与 vendor forward 行为一致）。
"""

from __future__ import annotations

import bisect
import logging
import math
from datetime import date, datetime
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

FactorEntry = tuple[date, float]


def normalize_factors(factors: Iterable[tuple] | None) -> list[FactorEntry]:
    """规整因子输入为 [(ex_date, factor)] 升序列表。

    接受 (date, factor) / (datetime, factor) / (str, factor) 元组；
    丢弃非有限或 <= 0 的因子（防御脏数据，记 warning）。
    """
    cleaned: list[FactorEntry] = []
    for item in factors or []:
        try:
            day_raw, value = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            logger.warning("dropping malformed ex-factor entry: %r", item)
            continue
        if isinstance(day_raw, datetime):
            day = day_raw.date()
        elif isinstance(day_raw, date):
            day = day_raw
        else:
            try:
                day = date.fromisoformat(str(day_raw)[:10])
            except ValueError:
                logger.warning("dropping ex-factor with unparseable date: %r", item)
                continue
        if not math.isfinite(value) or value <= 0:
            logger.warning("dropping non-positive/non-finite ex-factor %s on %s", value, day)
            continue
        cleaned.append((day, value))
    cleaned.sort(key=lambda entry: entry[0])
    return cleaned


def compute_qfq(raw: pd.DataFrame, factors: Iterable[tuple] | None) -> pd.DataFrame:
    """由 raw 日线 + 除权因子物化等比前复权（qfq）日线。

    参数:
        raw: 含 time/open/high/low/close/volume/amount 的不复权日线（任意排序）。
        factors: 可迭代的 (ex_date, ex_factor)。

    返回:
        与 raw 同结构的 DataFrame（按 time 升序），OHLC 已除权、
        volume/amount 原样保留；raw 为空时原样返回。
    """
    if raw is None or raw.empty:
        return raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame()

    out = raw.copy()
    out["time"] = pd.to_datetime(out["time"], errors="coerce")
    out = out.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    entries = normalize_factors(factors)
    if not entries:
        return out

    factor_dates = [entry[0] for entry in entries]
    # suffix_products[i] = Π_{j >= i} f_j —— bisect_left 定位后 O(1) 取累积积
    suffix_products = [1.0] * (len(entries) + 1)
    for idx in range(len(entries) - 1, -1, -1):
        suffix_products[idx] = suffix_products[idx + 1] * entries[idx][1]

    bar_dates = out["time"].dt.date
    divisors = bar_dates.map(
        lambda day: suffix_products[bisect.bisect_left(factor_dates, day)]
    )
    divisors = pd.to_numeric(divisors, errors="coerce").fillna(1.0)
    # 防御：累积积异常（0/负/非有限）时退化为 1，绝不让 qfq 出现非正价格
    divisors = divisors.where(divisors > 0, 1.0)

    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") / divisors
    return out


def factors_equal(old: Iterable[tuple] | None, new: Iterable[tuple] | None) -> bool:
    """比较两份因子表是否一致（用于除权变更检测）。"""
    old_entries = normalize_factors(old)
    new_entries = normalize_factors(new)
    if len(old_entries) != len(new_entries):
        return False
    return all(
        old_day == new_day and math.isclose(old_val, new_val, rel_tol=1e-9, abs_tol=1e-12)
        for (old_day, old_val), (new_day, new_val) in zip(old_entries, new_entries)
    )
