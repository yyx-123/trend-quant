"""标的大盘盘中快照：DB 存取 + 单例运行器守卫的单元测试。"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

import services.dashboard_snapshot as snapshot_module
from services.dashboard_snapshot import IntradaySnapshotRunner


# ---------------------------------------------------------------------------
# DB 层：快照表读写
# ---------------------------------------------------------------------------

def test_snapshot_save_load_roundtrip(test_db):
    payload = {"as_of": "2026-08-17", "groups": [{"category_l1": "ETF"}], "is_intraday": True}
    computed_at = test_db.save_dashboard_snapshot("intraday", "2026-08-17", payload)
    assert computed_at

    loaded = test_db.load_dashboard_snapshot()
    assert loaded is not None
    assert loaded["kind"] == "intraday"
    assert loaded["as_of"] == "2026-08-17"
    assert loaded["computed_at"] == computed_at
    assert loaded["payload"] == payload


def test_snapshot_save_replaces_single_row(test_db):
    test_db.save_dashboard_snapshot("intraday", "2026-08-17", {"v": 1})
    test_db.save_dashboard_snapshot("intraday", "2026-08-17", {"v": 2})
    loaded = test_db.load_dashboard_snapshot()
    assert loaded["payload"] == {"v": 2}
    with test_db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM dashboard_snapshot").fetchone()["c"]
    assert count == 1


def test_snapshot_load_empty(test_db):
    assert test_db.load_dashboard_snapshot() is None


def test_snapshot_never_touches_market_data(test_db):
    """快照写入不得触碰日K库（红线：market_data_* 只由收盘补库任务写入）。"""
    test_db.save_dashboard_snapshot("intraday", "2026-08-17", {"x": 1})
    with test_db._connect() as conn:
        for table in ("market_data_raw", "market_data_qfq"):
            count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            assert count == 0


# ---------------------------------------------------------------------------
# 运行器：触发守卫与单例并发
# ---------------------------------------------------------------------------

@pytest.fixture
def runner_env(test_db, monkeypatch):
    """隔离的运行器环境：临时库 + 可控的日历/行情函数。"""
    runner = IntradaySnapshotRunner()
    monkeypatch.setattr(snapshot_module, "get_db", lambda: test_db)
    return runner, monkeypatch


def _set_trading_moment(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(snapshot_module, "market_now", lambda: when)
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: True)
    monkeypatch.setattr(snapshot_module, "is_past_market_open", lambda dt=None: True)


def test_ensure_running_skips_non_trading_day(runner_env):
    runner, monkeypatch = runner_env
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: False)
    result = runner.ensure_running(trigger="test")
    assert result["status"] == "skipped"
    assert result["reason"] == "non_trading_day"


def test_ensure_running_skips_pre_open(runner_env):
    runner, monkeypatch = runner_env
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: True)
    monkeypatch.setattr(snapshot_module, "is_past_market_open", lambda dt=None: False)
    result = runner.ensure_running(trigger="test")
    assert result["status"] == "skipped"
    assert result["reason"] == "pre_open"


def test_ensure_running_skips_when_eod_current(runner_env, test_db, monkeypatch):
    """今日日K已落库（收盘补库完成）时无需实时估算。"""
    runner, monkeypatch = runner_env
    now = datetime(2026, 8, 17, 10, 0)
    _set_trading_moment(monkeypatch, now)
    monkeypatch.setattr(
        type(test_db),
        "get_market_dashboard_revision",
        lambda self: ("2026-08-17 00:00:00", 100, "", 1),
    )
    result = runner.ensure_running(trigger="test")
    assert result["status"] == "skipped"
    assert result["reason"] == "eod_current"


def _stub_compute(monkeypatch, test_db, payload, hold: threading.Event | None = None):
    """替换重算依赖：造假标的清单 + 假的盘中看板构建函数。"""
    monkeypatch.setattr(type(test_db), "list_market_symbols", lambda self, price_mode="qfq": ["510300.SS"])
    monkeypatch.setattr(type(test_db), "get_instrument_metadata_map", lambda self: {"510300.SS": {}})
    monkeypatch.setattr(snapshot_module, "filter_fully_classified", lambda symbols, meta: list(symbols))

    class _FakeDataService:
        def close(self):
            pass

    monkeypatch.setattr(snapshot_module, "DataService", _FakeDataService)
    monkeypatch.setattr(snapshot_module, "trend_config", lambda: {})

    def fake_build(symbols, db, data_service, trend_config, *, progress_callback=None):
        if hold is not None:
            hold.wait(timeout=10)
        if progress_callback:
            progress_callback({"percent": 1.0, "message": "完成"})
        return payload

    monkeypatch.setattr(snapshot_module, "build_intraday_dashboard", fake_build)


def _wait_done(runner, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not runner.status()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("runner did not finish in time")


def test_successful_run_saves_snapshot(runner_env, test_db, monkeypatch):
    runner, monkeypatch = runner_env
    _set_trading_moment(monkeypatch, datetime(2026, 8, 17, 10, 0))
    payload = {"as_of": "2026-08-17", "groups": [], "is_intraday": True}
    _stub_compute(monkeypatch, test_db, payload)

    result = runner.ensure_running(trigger="test")
    assert result["status"] == "started"
    _wait_done(runner)

    snapshot = runner.latest_snapshot()
    assert snapshot is not None
    assert snapshot["kind"] == "intraday"
    assert snapshot["as_of"] == "2026-08-17"
    assert snapshot["payload"] == payload
    # 持久化到 DB（进程重启后仍可懒加载）。
    assert test_db.load_dashboard_snapshot()["payload"] == payload
    status = runner.status()
    assert status["last_error"] is None
    assert status["snapshot_ts"] == snapshot["computed_at"]


def test_singleton_rejects_concurrent_run(runner_env, test_db, monkeypatch):
    """计算进行中再次触发（定时任务/多用户开页）必须复用而非多跑。"""
    runner, monkeypatch = runner_env
    _set_trading_moment(monkeypatch, datetime(2026, 8, 17, 10, 0))
    hold = threading.Event()
    payload = {"as_of": "2026-08-17", "groups": [], "is_intraday": True}
    _stub_compute(monkeypatch, test_db, payload, hold=hold)

    first = runner.ensure_running(trigger="page_open")
    assert first["status"] == "started"
    # 等工作线程真正进入构建函数（running 标志已置位）。
    second = runner.ensure_running(trigger="schedule")
    assert second["status"] == "running"

    hold.set()
    _wait_done(runner)
    assert runner.latest_snapshot()["payload"] == payload


def test_failed_run_keeps_previous_snapshot(runner_env, test_db, monkeypatch):
    runner, monkeypatch = runner_env
    _set_trading_moment(monkeypatch, datetime(2026, 8, 17, 10, 0))
    _stub_compute(monkeypatch, test_db, {"as_of": "2026-08-17", "v": "old"})
    runner.ensure_running(trigger="test")
    _wait_done(runner)

    def boom(**kwargs):
        raise RuntimeError("quote source down")

    monkeypatch.setattr(snapshot_module, "build_intraday_dashboard", lambda *a, **k: boom())
    runner.ensure_running(trigger="test")
    _wait_done(runner)

    assert runner.status()["last_error"] == "quote source down"
    # 旧快照仍可用于展示。
    assert runner.latest_snapshot()["payload"] == {"as_of": "2026-08-17", "v": "old"}
