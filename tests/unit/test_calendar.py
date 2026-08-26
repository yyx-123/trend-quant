"""Unit tests for core.calendar — trading day / time logic.

Mock ``chinese_calendar.is_workday`` to get deterministic results.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest


class TestIsTradingDay:
    def test_weekday_is_trading_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monday‑Friday should return True when chinese_calendar says so."""
        monkeypatch.setattr("core.calendar.is_workday", lambda d: True)
        from core.calendar import is_trading_day

        for d in (date(2025, 8, 11), date(2025, 8, 12), date(2025, 8, 13),
                  date(2025, 8, 14), date(2025, 8, 15)):
            assert is_trading_day(d) is True, f"{d} should be a trading day"

    def test_weekend_not_trading_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Saturday / Sunday return False."""
        monkeypatch.setattr("core.calendar.is_workday", lambda d: False)
        from core.calendar import is_trading_day

        assert is_trading_day(date(2025, 8, 9)) is False   # Saturday
        assert is_trading_day(date(2025, 8, 10)) is False  # Sunday

    def test_not_implemented_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When chinese_calendar raises NotImplementedError, fall back to
        weekday‑only check."""
        def _raise(d: date) -> bool:
            raise NotImplementedError
        monkeypatch.setattr("core.calendar.is_workday", _raise)
        # Reset warning-tracking so the test is deterministic
        monkeypatch.setattr("core.calendar._warned_years", set())

        from core.calendar import is_trading_day

        assert is_trading_day(date(2030, 7, 15)) is True   # Monday
        assert is_trading_day(date(2030, 7, 13)) is False  # Saturday


class TestIsTradingTime:
    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 29, False),    # before market open
        (9, 30, True),     # morning start
        (10, 0, True),     # morning middle
        (11, 30, True),    # morning end
        (11, 31, False),   # lunch break
        (12, 0, False),    # lunch break
        (13, 0, True),     # afternoon start
        (14, 30, True),    # afternoon middle
        (15, 0, True),     # afternoon end
        (15, 1, False),    # after close
    ])
    def test_trading_hours(self, hour: int, minute: int, expected: bool) -> None:
        from core.calendar import is_continuous_auction_hours

        dt = datetime(2025, 8, 11, hour, minute, 0)
        assert is_continuous_auction_hours(dt) == expected, f"Failed at {hour:02d}:{minute:02d}"


class TestIsRealtimeAvailable:
    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 29, False),    # before market open
        (9, 30, True),     # morning start
        (10, 0, True),     # morning middle
        (11, 30, True),    # morning end
        (11, 31, True),    # lunch break — still available
        (12, 0, True),     # lunch break — still available
        (12, 59, True),    # lunch break — still available
        (13, 0, True),     # afternoon start
        (14, 30, True),    # afternoon middle
        (15, 0, True),     # afternoon end
        (15, 1, False),    # after close
    ])
    def test_realtime_hours_include_lunch_break(
        self, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int, expected: bool
    ) -> None:
        monkeypatch.setattr("core.calendar.is_workday", lambda d: True)
        from core.calendar import is_realtime_available

        dt = datetime(2025, 8, 11, hour, minute, 0)  # Monday
        assert is_realtime_available(dt) == expected, f"Failed at {hour:02d}:{minute:02d}"

    def test_non_trading_day_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Weekend / holiday → False even during session hours."""
        monkeypatch.setattr("core.calendar.is_workday", lambda d: False)
        from core.calendar import is_realtime_available

        dt = datetime(2025, 8, 9, 10, 0, 0)  # Saturday 10:00
        assert is_realtime_available(dt) is False


class TestIsPastMarketOpen:
    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 29, False),    # before market open
        (9, 30, True),     # morning start
        (10, 0, True),     # morning middle
        (11, 30, True),    # morning end
        (12, 0, True),     # lunch break
        (13, 0, True),     # afternoon start
        (15, 0, True),     # close
        (15, 1, True),     # after close — still True (key difference from is_realtime_available)
        (16, 30, True),    # daily write job time
        (23, 59, True),    # late night, still the same trading day
    ])
    def test_past_open_hours(
        self, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int, expected: bool
    ) -> None:
        monkeypatch.setattr("core.calendar.is_workday", lambda d: True)
        from core.calendar import is_past_market_open

        dt = datetime(2025, 8, 11, hour, minute, 0)  # Monday
        assert is_past_market_open(dt) == expected, f"Failed at {hour:02d}:{minute:02d}"

    def test_non_trading_day_not_past_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Weekend / holiday → False even after 9:30."""
        monkeypatch.setattr("core.calendar.is_workday", lambda d: False)
        from core.calendar import is_past_market_open

        assert is_past_market_open(datetime(2025, 8, 9, 10, 0, 0)) is False  # Saturday
        assert is_past_market_open(datetime(2025, 8, 9, 16, 0, 0)) is False


class TestPreviousTradingDay:
    def test_saturday_returns_friday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """previous_trading_day(Saturday) → Friday."""
        def _is_workday(d: date) -> bool:
            return d.weekday() < 5  # simple weekday-only
        monkeypatch.setattr("core.calendar.is_workday", _is_workday)

        from core.calendar import previous_trading_day

        result = previous_trading_day(date(2025, 8, 9))  # Saturday
        assert result == date(2025, 8, 8)  # Friday

    def test_weekday_returns_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """previous_trading_day on a trading day returns the same day."""
        def _is_workday(d: date) -> bool:
            return d.weekday() < 5
        monkeypatch.setattr("core.calendar.is_workday", _is_workday)

        from core.calendar import previous_trading_day

        result = previous_trading_day(date(2025, 8, 13))  # Wednesday
        assert result == date(2025, 8, 13)


class TestNextTradingDay:
    def test_friday_returns_friday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _is_workday(d: date) -> bool:
            return d.weekday() < 5
        monkeypatch.setattr("core.calendar.is_workday", _is_workday)

        from core.calendar import next_trading_day

        result = next_trading_day(date(2025, 8, 15))  # Friday
        assert result == date(2025, 8, 15)

    def test_saturday_returns_monday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _is_workday(d: date) -> bool:
            return d.weekday() < 5
        monkeypatch.setattr("core.calendar.is_workday", _is_workday)

        from core.calendar import next_trading_day

        result = next_trading_day(date(2025, 8, 16))  # Saturday
        assert result == date(2025, 8, 18)  # Monday
