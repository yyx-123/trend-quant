"""登录接口暴力破解防护（P1-1）— 进程内滑动窗口限流 + 连续失败锁定。

口径（CR 修复方案 §P1-1，放宽口径）：
- 每 IP 登录接口 20 次/分钟（滑动窗口，正常用户远低于阈值）；
- 同 IP+用户名连续失败 10 次锁定 10 分钟（登录成功即清零计数）；
- 纯进程内状态：单实例部署下足够，重启即清零（可接受，爆破方同样重来）。
"""

from __future__ import annotations

import threading
import time
from collections import deque

IP_WINDOW_SECONDS = 60
IP_MAX_ATTEMPTS = 20
FAIL_MAX_CONSECUTIVE = 10
LOCK_SECONDS = 600

LOCKED_MESSAGE = "失败次数过多，已临时锁定，请 10 分钟后再试"
RATE_LIMITED_MESSAGE = "请求过于频繁，请稍后再试"


class LoginGuard:
    """登录尝试限流/锁定状态（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ip_hits: dict[str, deque[float]] = {}
        self._fail_counts: dict[tuple[str, str], int] = {}
        self._locked_until: dict[tuple[str, str], float] = {}
        self._checks_since_sweep = 0

    _SWEEP_EVERY = 256  # 每 256 次 check 全量清扫一次空闲条目（map 不无限增长）

    def _sweep_if_due(self, now: float) -> None:
        self._checks_since_sweep += 1
        if self._checks_since_sweep < self._SWEEP_EVERY:
            return
        self._checks_since_sweep = 0
        for ip in [ip for ip, hits in self._ip_hits.items() if not hits or now - hits[-1] > IP_WINDOW_SECONDS]:
            self._ip_hits.pop(ip, None)
        for key in [key for key, until in self._locked_until.items() if now >= until]:
            self._locked_until.pop(key, None)
        for key in [key for key, count in self._fail_counts.items() if count <= 0]:
            self._fail_counts.pop(key, None)

    def check(self, ip: str, username: str) -> str | None:
        """登记一次登录尝试；返回 None 放行，否则返回拒绝文案。"""
        now = time.monotonic()
        key = (ip, username)
        with self._lock:
            self._sweep_if_due(now)
            locked = self._locked_until.get(key)
            if locked is not None:
                if now < locked:
                    return LOCKED_MESSAGE
                del self._locked_until[key]
            hits = self._ip_hits.setdefault(ip, deque())
            while hits and now - hits[0] > IP_WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= IP_MAX_ATTEMPTS:
                return RATE_LIMITED_MESSAGE
            hits.append(now)
        return None

    def record_success(self, ip: str, username: str) -> None:
        with self._lock:
            self._fail_counts.pop((ip, username), None)
            self._locked_until.pop((ip, username), None)

    def record_failure(self, ip: str, username: str) -> bool:
        """登记一次失败；返回 True 表示本次失败触发了锁定。"""
        now = time.monotonic()
        key = (ip, username)
        with self._lock:
            # 已锁定期间不再累计（check 已拦截，正常不会走到这里）
            count = self._fail_counts.get(key, 0) + 1
            if count >= FAIL_MAX_CONSECUTIVE:
                self._locked_until[key] = now + LOCK_SECONDS
                self._fail_counts[key] = 0
                return True
            self._fail_counts[key] = count
        return False


# 模块级单例：登录路由与测试共用同一状态。
login_guard = LoginGuard()
