"""P1-1：登录限流（LoginGuard）+ P1-2/P2-23：备份与 SQLite 加固的单元测试。"""

from __future__ import annotations

import pytest

from services.login_guard import (
    FAIL_MAX_CONSECUTIVE,
    IP_MAX_ATTEMPTS,
    LOCKED_MESSAGE,
    RATE_LIMITED_MESSAGE,
    LoginGuard,
)


class TestLoginGuard:
    def test_under_limit_passes(self) -> None:
        guard = LoginGuard()
        for _ in range(IP_MAX_ATTEMPTS):
            assert guard.check("1.1.1.1", "u") is None
        # 第 21 次（窗口内）被限流
        assert guard.check("1.1.1.1", "u") == RATE_LIMITED_MESSAGE

    def test_ip_limit_is_per_ip(self) -> None:
        guard = LoginGuard()
        for _ in range(IP_MAX_ATTEMPTS):
            guard.check("1.1.1.1", "u")
        assert guard.check("2.2.2.2", "u") is None

    def test_ip_window_slides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.login_guard as lg

        now = [1000.0]
        fake_time = type("FakeTime", (), {"monotonic": staticmethod(lambda: now[0])})
        monkeypatch.setattr(lg, "time", fake_time)
        guard = LoginGuard()
        for _ in range(IP_MAX_ATTEMPTS):
            assert guard.check("1.1.1.1", "u") is None
        assert guard.check("1.1.1.1", "u") == RATE_LIMITED_MESSAGE
        now[0] += lg.IP_WINDOW_SECONDS + 1
        assert guard.check("1.1.1.1", "u") is None

    def test_consecutive_failures_lock(self) -> None:
        guard = LoginGuard()
        for i in range(FAIL_MAX_CONSECUTIVE):
            triggered = guard.record_failure("1.1.1.1", "u")
            assert triggered is (i == FAIL_MAX_CONSECUTIVE - 1)
        assert guard.check("1.1.1.1", "u") == LOCKED_MESSAGE
        # 锁定按 IP+用户名：同人不同 IP / 同 IP 不同人不受影响
        assert guard.check("2.2.2.2", "u") is None
        assert guard.check("1.1.1.1", "other") is None

    def test_success_resets_fail_count(self) -> None:
        guard = LoginGuard()
        for _ in range(FAIL_MAX_CONSECUTIVE - 1):
            guard.record_failure("1.1.1.1", "u")
        guard.record_success("1.1.1.1", "u")
        for i in range(FAIL_MAX_CONSECUTIVE - 1):
            assert guard.record_failure("1.1.1.1", "u") is False

    def test_lock_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.login_guard as lg

        now = [1000.0]
        fake_time = type("FakeTime", (), {"monotonic": staticmethod(lambda: now[0])})
        monkeypatch.setattr(lg, "time", fake_time)
        guard = LoginGuard()
        for _ in range(FAIL_MAX_CONSECUTIVE):
            guard.record_failure("1.1.1.1", "u")
        assert guard.check("1.1.1.1", "u") == LOCKED_MESSAGE
        now[0] += lg.LOCK_SECONDS + 1
        assert guard.check("1.1.1.1", "u") is None
