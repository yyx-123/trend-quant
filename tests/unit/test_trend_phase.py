"""core.trend_phase：多周期趋势相位（三态离散 + 持续天数）的 canonical 口径。

相位 = 趋势值相对阈值的三态离散（日 ±5、周/月 ±9），持续天数 = 截至最新一根
有效 bar 的同状态连续根数（转入当日为 1，无趋势同理）。
"""

from __future__ import annotations

import pytest

from core.bars import PERIOD_DAILY, PERIOD_MONTHLY, PERIOD_WEEKLY
from core.trend_phase import (
    PHASE_NEGATIVE,
    PHASE_NONE,
    PHASE_POSITIVE,
    PHASE_THRESHOLDS,
    period_state,
    phase_run,
    phase_state,
    phase_threshold,
    trend_periods,
)


class TestThresholds:
    def test_thresholds_per_period(self) -> None:
        assert PHASE_THRESHOLDS == {PERIOD_DAILY: 5.0, PERIOD_WEEKLY: 9.0, PERIOD_MONTHLY: 9.0}
        assert phase_threshold("1d") == 5.0
        assert phase_threshold("1w") == 9.0
        assert phase_threshold("1M") == 9.0

    @pytest.mark.parametrize(
        "alias,expected",
        [("daily", 5.0), ("d", 5.0), ("D", 5.0), ("weekly", 9.0), ("w", 9.0), ("monthly", 9.0), ("M", 9.0)],
    )
    def test_alias_normalization(self, alias: str, expected: float) -> None:
        assert phase_threshold(alias) == expected

    def test_unknown_period_rejected(self) -> None:
        with pytest.raises(ValueError):
            phase_threshold("5m")


class TestPhaseState:
    def test_three_states(self) -> None:
        assert phase_state(12.0, 9.0) == PHASE_POSITIVE
        assert phase_state(-12.0, 9.0) == PHASE_NEGATIVE
        assert phase_state(3.0, 9.0) == PHASE_NONE

    def test_threshold_is_inclusive_for_none(self) -> None:
        """阈值本身算「无趋势」（> τ 才判正、< -τ 才判负）。"""
        for threshold in (5.0, 9.0):
            assert phase_state(threshold, threshold) == PHASE_NONE
            assert phase_state(-threshold, threshold) == PHASE_NONE
            assert phase_state(threshold + 1e-9, threshold) == PHASE_POSITIVE
            assert phase_state(-threshold - 1e-9, threshold) == PHASE_NEGATIVE

    @pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "abc"])
    def test_missing_values(self, value) -> None:
        assert phase_state(value, 5.0) is None


class TestPhaseRun:
    def test_transition_day_is_one(self) -> None:
        """首次转入正趋势当日为第 1 天。"""
        run = phase_run([0.0, 6.0], threshold=5.0, dates=["2026-09-11", "2026-09-14"])
        assert run == {
            "phase": PHASE_POSITIVE,
            "previous_phase": PHASE_NONE,
            "phase_days": 1,
            "phase_since": "2026-09-14",
        }

    def test_run_length_counts_continuation(self) -> None:
        run = phase_run([6.0, 7.0, 0.0, 6.0, 8.0], threshold=5.0)
        assert run["phase"] == PHASE_POSITIVE
        assert run["phase_days"] == 2
        assert run["phase_since"] is None  # 未给 dates

    def test_none_and_negative_states_count_too(self) -> None:
        assert phase_run([9.0, 9.0, 9.0], threshold=9.0)["phase"] == PHASE_NONE
        assert phase_run([9.0, 9.0, 9.0], threshold=9.0)["phase_days"] == 3
        assert phase_run([-9.0, -9.0, -9.0], threshold=9.0)["phase"] == PHASE_NONE
        assert phase_run([-9.5, -9.5], threshold=9.0)["phase"] == PHASE_NEGATIVE
        assert phase_run([-9.5, -9.5], threshold=9.0)["phase_days"] == 2

    def test_since_is_first_bar_of_run(self) -> None:
        run = phase_run(
            [6.0, 6.0, -1.0, 7.0, 6.5],
            threshold=5.0,
            dates=["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14"],
        )
        assert run["phase_days"] == 2
        assert run["phase_since"] == "2026-09-11"
        # previous_phase 是「紧邻的前一根」，不是「本段相位之前的那一根」：
        # 本段已持续 2 天，则昨日仍在段内、与当前同相位。
        assert run["previous_phase"] == PHASE_POSITIVE

    def test_nan_breaks_the_run(self) -> None:
        run = phase_run([6.0, 6.0, float("nan"), 6.0], threshold=5.0)
        assert run["phase_days"] == 1
        # 紧邻的前一根是 NaN → 昨日相位未知（不跳过去取值）。
        assert run["previous_phase"] is None

    def test_leading_warmup_nan_is_ignored(self) -> None:
        run = phase_run([float("nan")] * 4 + [6.0, 7.0], threshold=5.0)
        assert run["phase"] == PHASE_POSITIVE
        assert run["phase_days"] == 2
        # 前一根（6.0，同为有效值）仍是正趋势；预热期 NaN 只截断了回扫。
        assert run["previous_phase"] == PHASE_POSITIVE

    def test_previous_phase_unknown_when_only_bar_is_first(self) -> None:
        run = phase_run([6.0, float("nan")], threshold=5.0)
        assert run == {
            "phase": PHASE_POSITIVE,
            "previous_phase": None,  # 前一根无有效值
            "phase_days": 1,
            "phase_since": None,
        }

    def test_trailing_nan_uses_last_valid(self) -> None:
        run = phase_run([6.0, 7.0, float("nan")], threshold=5.0)
        assert run["phase"] == PHASE_POSITIVE
        assert run["phase_days"] == 2

    def test_empty_and_all_nan(self) -> None:
        for values in ([], None, [float("nan"), None]):
            assert phase_run(values, threshold=5.0) == {
                "phase": None,
                "previous_phase": None,
                "phase_days": None,
                "phase_since": None,
            }

    def test_run_reaching_series_start(self) -> None:
        run = phase_run([6.0, 6.0, 6.0], threshold=5.0, dates=["a", "b", "c"])
        assert run["phase_days"] == 3
        assert run["phase_since"] == "a"


class TestPreviousPhase:
    """previous_phase：读「从什么相位变化到什么相位」（走强/走弱）的原料。"""

    def test_unchanged_phase_repeats_current(self) -> None:
        """相位未变的日子：前一根与当前同相位（消费方据此判断「无切换」）。"""
        run = phase_run([-12.0, 6.0, 7.0, 8.0], threshold=5.0)
        assert run["phase"] == PHASE_POSITIVE
        assert run["phase_days"] == 3
        assert run["previous_phase"] == PHASE_POSITIVE

    @pytest.mark.parametrize(
        "previous,current",
        [
            (-12.0, 6.0),  # 负 → 正：走强
            (0.0, 6.0),  # 无 → 正：走强
            (12.0, -6.0),  # 正 → 负：走弱
            (0.0, -6.0),  # 无 → 负：走弱
            (12.0, 0.0),  # 正 → 无：转震荡
            (-12.0, 0.0),  # 负 → 无
        ],
    )
    def test_switch_pairs_are_visible_on_transition_day(self, previous: float, current: float) -> None:
        run = phase_run([previous, current], threshold=5.0)
        assert run["phase_days"] == 1  # 切换当日必为第 1 天
        assert run["previous_phase"] == phase_state(previous, 5.0)
        assert run["previous_phase"] != run["phase"]

    def test_first_bar_has_no_previous(self) -> None:
        assert phase_run([6.0], threshold=5.0)["previous_phase"] is None

    def test_previous_phase_null_in_empty_bundle(self) -> None:
        assert period_state(None, PERIOD_DAILY)["previous_phase"] is None
        assert trend_periods()["monthly"]["previous_phase"] is None


class TestPeriodState:
    def test_bundle_shape_and_values(self) -> None:
        state = period_state([1.0, 10.5], PERIOD_WEEKLY, dates=["2026-09-04", "2026-09-11"])
        assert state == {
            "trend_score": 10.5,
            "threshold": 9.0,
            "phase": PHASE_POSITIVE,
            "previous_phase": PHASE_NONE,
            "phase_days": 1,
            "phase_since": "2026-09-11",
        }

    def test_trend_score_is_last_valid(self) -> None:
        state = period_state([6.0, 7.0, float("nan")], PERIOD_DAILY)
        assert state["trend_score"] == 7.0
        assert state["phase"] == PHASE_POSITIVE
        assert state["phase_days"] == 2

    def test_empty_series_yields_stable_shape(self) -> None:
        for values in (None, [], [float("nan")]):
            state = period_state(values, PERIOD_MONTHLY)
            assert state == {
                "trend_score": None,
                "threshold": 9.0,
                "phase": None,
                "previous_phase": None,
                "phase_days": None,
                "phase_since": None,
            }


class TestTrendPeriods:
    def test_all_three_dimensions_always_present(self) -> None:
        periods = trend_periods(
            daily=period_state([6.0], PERIOD_DAILY),
            weekly=period_state([10.0], PERIOD_WEEKLY),
        )
        assert list(periods) == ["daily", "weekly", "monthly"]
        assert periods["daily"]["phase"] == PHASE_POSITIVE
        assert periods["daily"]["threshold"] == 5.0
        assert periods["weekly"]["phase"] == PHASE_POSITIVE
        # 未提供（无数据）的维度以空 bundle 占位，形状稳定。
        assert periods["monthly"]["phase"] is None
        assert periods["monthly"]["threshold"] == 9.0

    def test_no_dimensions_provided(self) -> None:
        periods = trend_periods()
        assert set(periods) == {"daily", "weekly", "monthly"}
        assert all(periods[key]["phase"] is None for key in periods)
