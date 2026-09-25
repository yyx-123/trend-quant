"""R3 probe 4: 复现 r2 钉子的顺序依赖失败并取诊断数据。

用法：pytest 先跑 tests/api/test_research_ledger_api.py 再跑本文件。
"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime

import pytest


def test_sentinel_diagnostics(monkeypatch):
    from core import jobs
    from core import settings as settings_mod

    calls: list[int] = []
    sleeps: list[float] = []
    starts: list[str] = []

    def _fake_job(_settings, _service=None, force=False, after_update=None):
        calls.append(1)
        return {"status": "deferred_backtest_running"}

    real_thread_cls = threading.Thread

    class _FakeThread:
        def __init__(self, target=None, **kwargs):
            self._target = target
            self.daemon = kwargs.get("daemon", False)
            self.name = kwargs.get("name", "")
            self._alive = False

        def start(self):
            starts.append(self.name)
            self._alive = True
            try:
                self._target()
            finally:
                self._alive = False

        def is_alive(self):
            return self._alive

    before = {t.name for t in threading.enumerate()}
    sentinel_before = jobs._catchup_sentinel
    print("\n[diag] _catchup_sentinel before =", repr(sentinel_before))
    if sentinel_before is not None:
        try:
            print("[diag] is_alive =", sentinel_before.is_alive(),
                  "type =", type(sentinel_before))
        except Exception as exc:  # noqa: BLE001
            print("[diag] is_alive raised:", exc)
    monkeypatch.setattr(jobs, "daily_market_update_job", _fake_job)
    monkeypatch.setattr(jobs, "threading", type("T", (), {"Thread": _FakeThread}))
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(jobs.run_freeze, "is_frozen", lambda: False)
    monkeypatch.setattr(jobs, "market_now", lambda: datetime(2026, 9, 25, 17, 0))
    monkeypatch.setattr(jobs, "_catchup_sentinel", None)

    jobs._spawn_same_day_catchup(
        settings_mod.load_settings(), None, force=True, today=date(2026, 9, 25),
        after_update=None,
    )
    after = {t.name for t in threading.enumerate()}
    print(f"[diag] calls={len(calls)} sleeps={len(sleeps)} starts={starts}")
    print(f"[diag] new threads during test = {sorted(after - before)}")
    print(f"[diag] all live threads = {sorted((t.name, t.is_alive()) for t in threading.enumerate())}")
    real = [t for t in threading.enumerate() if t is not threading.current_thread()]
    print(f"[diag] sentinel after = {type(jobs._catchup_sentinel).__name__}")
    assert True
