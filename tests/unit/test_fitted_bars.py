"""拟合周/月K 聚合（core.bars.fitted_period_rows）。

核心语义：拟合行 t 只依赖 t 所在周期内 ≤t 的日K（PIT，无周期内前视）；
open=周期首日、high/low=累计极值、close=当日、volume/amount=累计。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.bars import FITTED_COLUMNS, fitted_period_rows


def _daily(rows: list[tuple]) -> pd.DataFrame:
    """rows: (date, open, high, low, close, volume)"""
    return pd.DataFrame(
        [
            {
                "time": d,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
                "amount": v * c,
            }
            for d, o, h, lo, c, v in rows
        ]
    )


class TestFittedWeekly:
    def test_cumulative_within_week(self) -> None:
        # 2026-09-07 是周一：两根日K → 第二根的拟合应累计前两日
        df = _daily(
            [
                ("2026-09-07", 10.0, 10.5, 9.8, 10.2, 100.0),
                ("2026-09-08", 10.3, 10.4, 10.0, 10.1, 200.0),
                ("2026-09-09", 10.2, 10.8, 10.1, 10.6, 150.0),
            ]
        )
        out = fitted_period_rows(df, "1w")
        assert list(out.columns) == FITTED_COLUMNS
        assert len(out) == 3
        # 第一天：open/high/low/close 就是当天
        first = out.iloc[0]
        assert (first["open"], first["high"], first["low"], first["close"]) == (10.0, 10.5, 9.8, 10.2)
        assert first["volume"] == 100.0
        assert first["period_start"] == "2026-09-07"
        # 第三天：open 仍是周一的，high 累计到 10.8，low 仍是 9.8，量累计 450
        third = out.iloc[2]
        assert (third["open"], third["high"], third["low"], third["close"]) == (10.0, 10.8, 9.8, 10.6)
        assert third["volume"] == 450.0
        assert third["amount"] == pytest.approx(100 * 10.2 + 200 * 10.1 + 150 * 10.6)

    def test_week_boundary_resets(self) -> None:
        df = _daily(
            [
                ("2026-09-11", 10.0, 10.5, 9.9, 10.4, 100.0),  # 周五（当周第 4 个交易日）
                ("2026-09-14", 20.0, 20.5, 19.8, 20.2, 300.0),  # 下周一：新周期重新累计
            ]
        )
        out = fitted_period_rows(df, "1w")
        assert out.iloc[1]["period_start"] == "2026-09-14"
        assert (out.iloc[1]["open"], out.iloc[1]["high"], out.iloc[1]["volume"]) == (20.0, 20.5, 300.0)

    def test_iso_year_boundary_week(self) -> None:
        # 2026-01-01（周四）属 ISO 2026-W01，该周周一是 2025-12-29
        df = _daily(
            [
                ("2025-12-29", 5.0, 5.2, 4.9, 5.1, 50.0),
                ("2026-01-01", 5.1, 5.3, 5.0, 5.2, 60.0),
            ]
        )
        out = fitted_period_rows(df, "1w")
        assert out.iloc[1]["period_start"] == "2025-12-29"
        assert out.iloc[1]["volume"] == 110.0  # 跨年同一周期，量连续累计

    def test_suspension_gap_keeps_cumulation(self) -> None:
        # 周三、周四停牌（无日K行）：拟合行只按已有日K累计，停牌日不产生行
        df = _daily(
            [
                ("2026-09-07", 10.0, 10.5, 9.8, 10.2, 100.0),
                ("2026-09-08", 10.2, 10.3, 10.0, 10.1, 100.0),
                ("2026-09-11", 10.1, 10.4, 10.0, 10.3, 100.0),
            ]
        )
        out = fitted_period_rows(df, "1w")
        assert len(out) == 3  # 无 09-09/09-10 行
        assert out.iloc[2]["time"].strftime("%Y-%m-%d") == "2026-09-11"
        assert out.iloc[2]["volume"] == 300.0


class TestFittedMonthly:
    def test_cumulative_within_month_and_reset(self) -> None:
        df = _daily(
            [
                ("2026-08-31", 8.0, 8.2, 7.9, 8.1, 10.0),
                ("2026-09-01", 8.1, 8.3, 8.0, 8.2, 10.0),
                ("2026-09-02", 8.2, 8.4, 8.1, 8.3, 10.0),
            ]
        )
        out = fitted_period_rows(df, "1M")
        assert out.iloc[0]["period_start"] == "2026-08-01"
        assert out.iloc[1]["period_start"] == "2026-09-01"
        assert out.iloc[1]["volume"] == 10.0  # 新周期重新累计
        assert out.iloc[2]["volume"] == 20.0
        assert out.iloc[2]["open"] == 8.1

    def test_listing_mid_month_partial_period(self) -> None:
        # 上市首月只有从 15 号起的日K：半截周期如实保留（PIT 语义）
        df = _daily(
            [
                ("2026-07-15", 3.0, 3.1, 2.9, 3.05, 5.0),
                ("2026-07-16", 3.05, 3.2, 3.0, 3.15, 6.0),
            ]
        )
        out = fitted_period_rows(df, "1M")
        assert len(out) == 2
        assert out.iloc[0]["period_start"] == "2026-07-01"
        assert out.iloc[1]["open"] == 3.0
        assert out.iloc[1]["volume"] == 11.0


class TestFittedInputHandling:
    def test_unsorted_input_is_sorted(self) -> None:
        df = _daily(
            [
                ("2026-09-08", 10.3, 10.4, 10.0, 10.1, 200.0),
                ("2026-09-07", 10.0, 10.5, 9.8, 10.2, 100.0),
            ]
        )
        out = fitted_period_rows(df, "1w")
        assert out.iloc[1]["volume"] == 300.0  # 按时间序累计，而非入参顺序

    def test_empty_input(self) -> None:
        out = fitted_period_rows(pd.DataFrame(), "1w")
        assert out.empty and list(out.columns) == FITTED_COLUMNS

    def test_daily_period_rejected(self) -> None:
        with pytest.raises(ValueError):
            fitted_period_rows(_daily([("2026-09-07", 1, 1, 1, 1, 1)]), "1d")

    def test_amount_missing_gives_nan(self) -> None:
        df = _daily([("2026-09-07", 10.0, 10.5, 9.8, 10.2, 100.0)]).drop(columns="amount")
        out = fitted_period_rows(df, "1w")
        assert pd.isna(out.iloc[0]["amount"])

    def test_does_not_mutate_input(self) -> None:
        df = _daily([("2026-09-07", 10.0, 10.5, 9.8, 10.2, 100.0)])
        original = df.copy(deep=True)
        fitted_period_rows(df, "1w")
        pd.testing.assert_frame_equal(df, original)
