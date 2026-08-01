"""core.adjustment 单元测试：等比 qfq 物化与因子 diff。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.adjustment import compute_qfq, factors_equal, normalize_factors


def _raw_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]),
            "open": [10.0, 20.0, 5.0, 5.1],
            "high": [10.5, 20.5, 5.2, 5.2],
            "low": [9.5, 19.5, 4.8, 5.0],
            "close": [10.0, 20.0, 5.0, 5.1],
            "volume": [1000, 2000, 3000, 3100],
            "amount": [10000, 40000, 15000, 15810],
        }
    )


class TestComputeQfq:
    def test_no_factors_returns_raw_sorted(self) -> None:
        raw = _raw_bars().iloc[::-1]  # 乱序输入
        out = compute_qfq(raw, [])
        pd.testing.assert_frame_equal(out.reset_index(drop=True), _raw_bars())

    def test_forward_adjust_divides_bars_on_and_before_ex_date(self) -> None:
        # 2026-01-07 除权，因子 2.0（如 10 转 10）：当日及之前的 bar 除以 2
        out = compute_qfq(_raw_bars(), [(date(2026, 1, 7), 2.0)])
        closes = out["close"].tolist()
        assert closes == pytest.approx([5.0, 10.0, 2.5, 5.1])
        # volume / amount 不调整（与 vendor forward 行为一致）
        assert out["volume"].tolist() == [1000, 2000, 3000, 3100]
        assert out["amount"].tolist() == [10000, 40000, 15000, 15810]

    def test_multiple_factors_compound(self) -> None:
        # 两次除权：01-06 ×2、01-08 ×1.5 → 除数 = Π_{ex_date >= t} f（除权日当日也除）
        # 01-05: 3；01-06: 3；01-07: 1.5；01-08: 1.5
        factors = [(date(2026, 1, 6), 2.0), (date(2026, 1, 8), 1.5)]
        out = compute_qfq(_raw_bars(), factors)
        closes = out["close"].tolist()
        assert closes == pytest.approx([10.0 / 3.0, 20.0 / 3.0, 5.0 / 1.5, 5.1 / 1.5])

    def test_qfq_never_negative_for_positive_raw(self) -> None:
        factors = [(date(2026, 1, 6), 2.0), (date(2026, 1, 7), 1.4), (date(2026, 1, 8), 1.1)]
        out = compute_qfq(_raw_bars(), factors)
        assert (out[["open", "high", "low", "close"]] > 0).all().all()

    def test_invalid_factors_dropped(self) -> None:
        out = compute_qfq(_raw_bars(), [(date(2026, 1, 7), 0.0), (date(2026, 1, 7), -2.0), ("bad", 1.5)])
        assert out["close"].tolist() == [10.0, 20.0, 5.0, 5.1]

    def test_empty_raw(self) -> None:
        out = compute_qfq(pd.DataFrame(), [(date(2026, 1, 7), 2.0)])
        assert out.empty

    def test_ex_date_on_non_trading_day_applies_to_all_prior_bars(self) -> None:
        # 因子日期落在非交易日（vendor 实测存在，如 2025-10-19 周日）：
        # 周六 01-10 的因子 → 之前的全部 bar（01-05~01-08）都除
        out = compute_qfq(_raw_bars(), [(date(2026, 1, 10), 2.0)])
        assert out["close"].tolist() == pytest.approx([5.0, 10.0, 2.5, 2.55])

    def test_factor_before_first_bar_leaves_series_unchanged(self) -> None:
        # 因子日期早于首根 bar（已除权完成的历史）→ 不调整
        out = compute_qfq(_raw_bars(), [(date(2026, 1, 4), 2.0)])
        assert out["close"].tolist() == [10.0, 20.0, 5.0, 5.1]


class TestNormalizeFactors:
    def test_accepts_str_and_datetime(self) -> None:
        entries = normalize_factors([("2026-01-07", 2.0), (pd.Timestamp("2026-01-06"), 1.5)])
        assert entries == [(date(2026, 1, 6), 1.5), (date(2026, 1, 7), 2.0)]


class TestFactorsEqual:
    def test_equal_across_types(self) -> None:
        assert factors_equal([("2026-01-07", 2.0)], [(date(2026, 1, 7), 2.0)]) is True

    def test_detects_added_factor(self) -> None:
        assert factors_equal([("2026-01-07", 2.0)], [("2026-01-07", 2.0), ("2026-06-01", 1.1)]) is False

    def test_detects_changed_factor(self) -> None:
        assert factors_equal([("2026-01-07", 2.0)], [("2026-01-07", 2.1)]) is False
