"""core/bars.py 周期口径：别名规范化 + 「未走完的周期不落库」判定。

关键回归点：vendor 的周/月 bar 标注日是该标的在本周期内最后一个有成交的
日子，停牌会让标注日落在周期中间（万科A 2015-12 月 bar 标 12-18、招行
2015-04 周 bar 标 04-02、万科A 2017-06 周 bar 标 06-06）。这些 bar 是
完整周期，必须保留 —— 早期版本按「标注日 + 下一交易日是否跨周期」判断，
把它们误判为进行中并丢弃。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from core.bars import (
    PERIOD_DAILY,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    closed_bars,
    date_span,
    is_period_bar_closed,
    normalize_period,
)


class TestNormalizePeriod:
    def test_alias_mapping(self) -> None:
        assert normalize_period("1d") == PERIOD_DAILY
        assert normalize_period("daily") == PERIOD_DAILY
        assert normalize_period("W") == PERIOD_WEEKLY
        assert normalize_period("weekly") == PERIOD_WEEKLY
        assert normalize_period("1w") == PERIOD_WEEKLY
        assert normalize_period("M") == PERIOD_MONTHLY
        assert normalize_period("monthly") == PERIOD_MONTHLY

    def test_none_and_blank_default_to_daily(self) -> None:
        assert normalize_period(None) == PERIOD_DAILY
        assert normalize_period("") == PERIOD_DAILY

    def test_minute_period_is_rejected(self) -> None:
        """小写 '1m' 是分钟线，绝不能静默落到月K。"""
        for value in ("1m", "m", "5m", "60m"):
            try:
                normalize_period(value)
            except ValueError:
                continue
            raise AssertionError(f"{value} should be rejected")

    def test_unknown_period_is_rejected(self) -> None:
        for value in ("hour", "1y", "weekly2"):
            try:
                normalize_period(value)
            except ValueError:
                continue
            raise AssertionError(f"{value} should be rejected")


class TestIsPeriodBarClosed:
    def test_daily_is_always_closed(self) -> None:
        # 日线不参与该判定：日 bar 的落库节奏由既有日更流程决定
        assert is_period_bar_closed(date(2026, 9, 11), "1d") is True

    def test_past_periods_are_closed(self) -> None:
        now = datetime(2026, 9, 12, 17, 30)  # 周六盘后
        for day, period in (
            ("2024-02-08", PERIOD_WEEKLY),  # 春节前最后交易日（该周只有 4 天）
            ("2015-04-02", PERIOD_WEEKLY),  # 招行定增停牌，周 bar 标在周四
            ("2017-06-06", PERIOD_WEEKLY),  # 万科A 停牌，周 bar 标在周二
            ("2015-12-18", PERIOD_MONTHLY),  # 万科A 停牌，月 bar 标在月中
            ("2015-09-30", PERIOD_WEEKLY),  # 国庆前短周
            ("2026-09-11", PERIOD_WEEKLY),  # 刚走完的一周（周六看已收盘）
            ("2026-08-31", PERIOD_MONTHLY),
        ):
            assert is_period_bar_closed(date.fromisoformat(day), period, now=now) is True, (day, period)

    def test_current_month_bar_is_not_closed(self) -> None:
        now = datetime(2026, 9, 12, 17, 30)
        assert is_period_bar_closed(date(2026, 9, 11), PERIOD_MONTHLY, now=now) is False

    def test_current_week_bar_waits_for_close(self) -> None:
        friday = date(2026, 9, 11)
        # 周五盘中：本周最后一根 bar 还没收盘
        assert is_period_bar_closed(friday, PERIOD_WEEKLY, now=datetime(2026, 9, 11, 11, 0)) is False
        # 周五盘后：可以落库
        assert is_period_bar_closed(friday, PERIOD_WEEKLY, now=datetime(2026, 9, 11, 16, 30)) is True
        # 周三盘中：周 bar 标注日就是当天 → 本周未走完
        assert is_period_bar_closed(date(2026, 9, 9), PERIOD_WEEKLY, now=datetime(2026, 9, 9, 15, 30)) is False
        # 周一（周末后）看上周五的 bar
        assert is_period_bar_closed(friday, PERIOD_WEEKLY, now=datetime(2026, 9, 14, 10, 0)) is True

    def test_holiday_week_is_closed_once_no_trading_day_left(self) -> None:
        # 国庆假期中（2025-10-04 周六）：9-30 那根短周 bar 已经走完
        assert (
            is_period_bar_closed(date(2025, 9, 30), PERIOD_WEEKLY, now=datetime(2025, 10, 4, 12, 0))
            is True
        )
        # 假期中看 9 月的月 bar：9 月已结束（10-04 已在 10 月）→ 已收盘
        assert (
            is_period_bar_closed(date(2025, 9, 30), PERIOD_MONTHLY, now=datetime(2025, 10, 4, 12, 0))
            is True
        )
        # 9-30 当天盘中看 9 月月 bar：本期（9 月）还没走完 → 不落库
        assert (
            is_period_bar_closed(date(2025, 9, 30), PERIOD_MONTHLY, now=datetime(2025, 9, 30, 12, 0))
            is False
        )
        # 9-30 盘后（9 月最后一个交易日、且已收盘）→ 可落库
        assert (
            is_period_bar_closed(date(2025, 9, 30), PERIOD_MONTHLY, now=datetime(2025, 9, 30, 16, 30))
            is True
        )

    def test_future_bar_is_not_closed(self) -> None:
        now = datetime(2026, 9, 12, 17, 30)
        assert is_period_bar_closed(date(2026, 9, 30), PERIOD_MONTHLY, now=now) is False
        assert is_period_bar_closed(date(2026, 12, 31), PERIOD_WEEKLY, now=now) is False


class TestClosedBars:
    @staticmethod
    def _frame(days: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "time": pd.to_datetime(days),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "amount": 1.0,
            }
        )

    def test_daily_passthrough(self) -> None:
        frame = self._frame(["2026-09-10", "2026-09-11"])
        assert closed_bars(frame, "1d") is frame

    def test_drops_only_open_period(self) -> None:
        now = datetime(2026, 9, 12, 17, 30)
        frame = self._frame(["2026-07-31", "2026-08-31", "2026-09-11"])
        kept = closed_bars(frame, PERIOD_MONTHLY, now=now)
        assert [str(day)[:10] for day in kept["time"]] == ["2026-07-31", "2026-08-31"]

    def test_empty_frame_and_missing_column(self) -> None:
        assert closed_bars(pd.DataFrame(), PERIOD_WEEKLY).empty
        assert closed_bars(self._frame(["2026-08-28"]).drop(columns=["time"]), PERIOD_WEEKLY).shape[0] == 1

    def test_input_not_mutated(self) -> None:
        now = datetime(2026, 9, 12, 17, 30)
        frame = self._frame(["2026-08-31", "2026-09-11"])
        closed_bars(frame, PERIOD_MONTHLY, now=now)
        assert len(frame) == 2


def test_date_span_unchanged() -> None:
    frame = pd.DataFrame({"time": pd.to_datetime(["2026-09-11", "2024-02-08"])})
    assert date_span(frame) == ("2024-02-08", "2026-09-11")
    assert date_span(pd.DataFrame()) == (None, None)
