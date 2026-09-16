"""services.dashboard_common 多周期趋势相位索引：取数 + 成交额加权聚合。

索引只产出周/月两个维度（日线维度各调用方自己就有序列），键一律为元组：
标的层 ``(symbol,)``、L3 ``(l1, l2, l3)``、L2 ``(l1, l2)``。类目层的聚合口径
与看板其余指标一致（成交额加权），标的层直接用自身滚动序列。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.trend_phase import PHASE_NEGATIVE, PHASE_NONE, PHASE_POSITIVE
from services.dashboard_common import (
    PHASE_LOOKBACK_DAYS,
    build_period_phase_index,
    load_period_trend_frame,
)

DATES = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


def _market_frame(amounts: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": pd.to_datetime(DATES),
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 1000.0,
            "amount": amounts,
        }
    )


def _rolling_frame(weekly: list[float], monthly: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"time": pd.to_datetime(DATES), "w_trend": weekly, "m_trend": monthly}
    )


def _seed(db, symbol: str, amounts: list[float], weekly: list[float], monthly: list[float]) -> None:
    db.save_market_data(symbol, _market_frame(amounts))
    db.save_rolling_trend_many([(symbol, _rolling_frame(weekly, monthly))])


class TestInstrumentLevel:
    def test_uses_own_rolling_series(self, test_db) -> None:
        _seed(test_db, "AAA.SS", [1000.0] * 4, [1.0, 2.0, 3.0, 12.0], [-20.0] * 4)
        index = build_period_phase_index(test_db, {"AAA.SS": ("ETF", "宽基", "沪深300")})

        bundle = index["instruments"][("AAA.SS",)]
        # 周：最后一天首次转入正趋势（>9）→ 持续 1 天，昨日为无趋势。
        assert bundle["weekly"] == {
            "trend_score": 12.0,
            "threshold": 9.0,
            "phase": PHASE_POSITIVE,
            "previous_phase": PHASE_NONE,
            "phase_days": 1,
            "phase_since": "2026-09-11",
        }
        # 月：全程 <-9 → 负趋势 4 天，首日即段的起点；昨日（段内）同为负。
        assert bundle["monthly"]["phase"] == PHASE_NEGATIVE
        assert bundle["monthly"]["previous_phase"] == PHASE_NEGATIVE
        assert bundle["monthly"]["phase_days"] == 4
        assert bundle["monthly"]["phase_since"] == "2026-09-08"

    def test_untracked_symbol_absent(self, test_db) -> None:
        _seed(test_db, "AAA.SS", [1000.0] * 4, [1.0, 2.0, 3.0, 12.0], [0.0] * 4)
        index = build_period_phase_index(
            test_db,
            {"AAA.SS": ("ETF", "宽基", "沪深300"), "BBB.SS": ("ETF", "宽基", "沪深300")},
        )
        assert ("AAA.SS",) in index["instruments"]
        # 无滚动行的标的不出现在索引里 —— 消费方回落到空 bundle 占位。
        assert ("BBB.SS",) not in index["instruments"]


class TestCategoryAggregation:
    def test_weighted_not_simple_average(self, test_db) -> None:
        """类目相位来自成交额加权的成员序列：权重悬殊时与简单平均结论相反。"""
        _seed(test_db, "BIG.SS", [1000.0] * 4, [12.0] * 4, [12.0] * 4)
        _seed(test_db, "SMALL.SS", [1.0] * 4, [-12.0] * 4, [-12.0] * 4)
        categories = {
            "BIG.SS": ("ETF", "宽基", "沪深300"),
            "SMALL.SS": ("ETF", "宽基", "沪深300"),
        }
        index = build_period_phase_index(test_db, categories)

        bundle = index["l3"][("ETF", "宽基", "沪深300")]
        expected = (12.0 * 1000.0 + -12.0 * 1.0) / 1001.0
        assert bundle["weekly"]["trend_score"] == pytest.approx(expected)
        # 简单平均为 0（无趋势），成交额加权后为正趋势 —— 证明用的是加权口径。
        assert bundle["weekly"]["phase"] == PHASE_POSITIVE
        assert bundle["weekly"]["phase_days"] == 4
        assert bundle["weekly"]["previous_phase"] == PHASE_POSITIVE
        assert bundle["monthly"]["phase"] == PHASE_POSITIVE

        # L2 是 L3 的父层：同一批成员，聚合结果一致。
        assert index["l2"][("ETF", "宽基")]["weekly"]["phase"] == PHASE_POSITIVE

    def test_category_without_weight_is_none(self, test_db) -> None:
        """成交额缺失的成员当日不计入聚合 → 全缺失时该日为 NaN（相位无值）。"""
        _seed(test_db, "AAA.SS", [0.0] * 4, [12.0] * 4, [12.0] * 4)
        index = build_period_phase_index(test_db, {"AAA.SS": ("ETF", "宽基", "沪深300")})
        bundle = index["l3"][("ETF", "宽基", "沪深300")]
        assert bundle["weekly"]["phase"] is None
        assert bundle["weekly"]["trend_score"] is None
        # 标的层不聚合，照常给出自身序列的相位。
        assert index["instruments"][("AAA.SS",)]["weekly"]["phase"] == PHASE_POSITIVE


class TestEmptySources:
    def test_empty_rolling_table(self, test_db) -> None:
        index = build_period_phase_index(test_db, {"AAA.SS": ("ETF", "宽基", "沪深300")})
        assert index == {"instruments": {}, "l3": {}, "l2": {}}

    def test_empty_symbol_map(self, test_db) -> None:
        assert build_period_phase_index(test_db, {}) == {"instruments": {}, "l3": {}, "l2": {}}

    def test_load_frame_requires_symbols(self, test_db) -> None:
        frame = load_period_trend_frame(test_db, [])
        assert frame.empty
        assert list(frame.columns) == ["symbol", "time", "weekly", "monthly", "amount"]


class TestLookbackWindow:
    def test_window_constant_covers_measured_runs(self, test_db) -> None:
        """回扫窗口 ≈410 个交易日，足以覆盖滚动月状态实测最长持续段（296 天）。"""
        assert PHASE_LOOKBACK_DAYS >= 500

    def test_old_rows_are_outside_window(self, test_db) -> None:
        stale = pd.DataFrame(
            {
                "time": pd.to_datetime(["2015-01-05", "2015-01-06"]),
                "w_trend": [12.0, 12.0],
                "m_trend": [12.0, 12.0],
            }
        )
        test_db.save_market_data("OLD.SS", _market_frame([1000.0] * 4))
        test_db.save_rolling_trend_many([("OLD.SS", stale)])
        index = build_period_phase_index(test_db, {"OLD.SS": ("ETF", "宽基", "沪深300")})
        assert ("OLD.SS",) not in index["instruments"]
