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
        lambda self: ("2026-08-17 00:00:00", "", 1),
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

    monkeypatch.setattr(snapshot_module, "get_data_service", lambda: _FakeDataService())
    monkeypatch.setattr(snapshot_module, "trend_config", dict)

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
    """计算进行中再次触发（相邻两轮定时任务）必须复用而非多跑。"""
    runner, monkeypatch = runner_env
    _set_trading_moment(monkeypatch, datetime(2026, 8, 17, 10, 0))
    hold = threading.Event()
    payload = {"as_of": "2026-08-17", "groups": [], "is_intraday": True}
    _stub_compute(monkeypatch, test_db, payload, hold=hold)

    first = runner.ensure_running(trigger="schedule")
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


# ---------------------------------------------------------------------------
# 读取口径：intraday_dashboard_snapshot（MCP 工具的唯一数据来源）
# ---------------------------------------------------------------------------

def _snapshot_payload() -> dict:
    l3a = {"category_l3": "沪深300", "children": [{"symbol": "510300.SS", "trend_history": [1.0] * 61}]}
    l3b = {"category_l3": "中证500", "children": [{"symbol": "510500.SS", "trend_history": [1.0] * 61}]}
    l2 = {"category_l2": "宽基", "children": [l3a, l3b], "child_count": 2}
    l2b = {
        "category_l2": "跨境",
        "children": [{"category_l3": "纳指", "children": [{"symbol": "513100.SS"}]}],
        "child_count": 1,
    }
    group = {"category_l1": "ETF", "items": [l2, l2b], "count": 2}
    return {
        "as_of": "2026-08-17T10:00:00",
        "groups": [group],
        "secondary_count": 2,
        "category_count": 3,
        "instrument_count": 3,
        "is_intraday": True,
        "intraday_ts": "2026-08-17T10:00:00",
    }


class _FakeSnapshotDb:
    def __init__(self, snapshot: dict | None) -> None:
        self._snapshot = snapshot

    def load_dashboard_snapshot(self) -> dict | None:
        return self._snapshot


def _read_env(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    snapshot: dict | None,
    *,
    realtime: bool = True,
) -> None:
    """隔离的读取环境：可控日历 + 手工构造的快照。"""
    monkeypatch.setattr(snapshot_module, "get_db", lambda: _FakeSnapshotDb(snapshot))
    monkeypatch.setattr(snapshot_module, "market_now", lambda: now)
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: True)
    monkeypatch.setattr(snapshot_module, "is_past_market_open", lambda dt=None: True)
    monkeypatch.setattr(snapshot_module, "is_realtime_available", lambda dt=None: realtime)


def _snapshot_row(payload: dict, computed_at: str) -> dict:
    return {"kind": "intraday", "as_of": payload["as_of"], "computed_at": computed_at, "payload": payload}


class TestIntradayDashboardSnapshotRead:
    def test_non_trading_day(self, monkeypatch) -> None:
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 0), None)
        monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: False)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is False
        assert "非交易日" in result["error"]

    def test_pre_open(self, monkeypatch) -> None:
        _read_env(monkeypatch, datetime(2026, 8, 17, 9, 0), None)
        monkeypatch.setattr(snapshot_module, "is_past_market_open", lambda dt=None: False)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is False
        assert "尚未开盘" in result["error"]

    def test_no_snapshot_yet(self, monkeypatch) -> None:
        """9:30~9:35 首轮未跑完 / 进程刚部署：无今日快照。"""
        _read_env(monkeypatch, datetime(2026, 8, 17, 9, 32), None)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is False
        assert "尚未生成" in result["error"]

    def test_yesterdays_snapshot_rejected(self, monkeypatch) -> None:
        payload = dict(_snapshot_payload(), as_of="2026-08-14T15:00:00")
        row = _snapshot_row(payload, "2026-08-14T15:00:20")  # 上一交易日
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 0), row)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is False
        assert "尚未生成" in result["error"]

    def test_fresh_snapshot_served(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is True
        assert result["source"] == "snapshot"
        assert result["snapshot_ts"] == "2026-08-17T10:00:00"
        assert result["post_close"] is False
        assert result["instrument_count"] == 3
        # 原 payload 字段透传
        assert result["as_of"] == "2026-08-17T10:00:00"

    def test_stale_snapshot_in_active_window_rejected(self, monkeypatch) -> None:
        """调度活跃窗口内快照超过 10 分钟未更新 → 定时任务疑似停摆。"""
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T09:40:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 0), row)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is False
        assert "过期" in result["error"]

    def test_lunch_break_accepts_morning_snapshot(self, monkeypatch) -> None:
        """午间休盘（11:35~13:00）不在活跃窗口内：接受 11:30 的上午快照。"""
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T11:30:20")
        _read_env(monkeypatch, datetime(2026, 8, 17, 12, 30), row)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is True

    def test_post_close_accepts_close_snapshot(self, monkeypatch) -> None:
        """15:05 后不再有时效要求：收盘快照服役到当日日K落库。"""
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T15:00:20")
        _read_env(monkeypatch, datetime(2026, 8, 17, 16, 0), row, realtime=False)
        result = snapshot_module.intraday_dashboard_snapshot()
        assert result["ok"] is True
        assert result["post_close"] is True

    def test_category_filter_l2_keeps_subtree(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.intraday_dashboard_snapshot(category="宽基")
        assert result["ok"] is True
        assert result["requested_category"] == "宽基"
        assert result["secondary_count"] == 1
        assert result["category_count"] == 2
        assert result["instrument_count"] == 2

    def test_category_filter_l3_prunes(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.intraday_dashboard_snapshot(category="沪深300")
        assert result["ok"] is True
        assert result["secondary_count"] == 1
        assert result["category_count"] == 1
        assert result["instrument_count"] == 1
        l2 = result["groups"][0]["items"][0]
        assert l2["category_l2"] == "宽基"
        assert l2["child_count"] == 1

    def test_category_filter_no_match(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.intraday_dashboard_snapshot(category="不存在")
        assert result["ok"] is False
        assert "不存在" in result["error"]

    def test_category_filter_does_not_mutate_snapshot(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        snapshot_module.intraday_dashboard_snapshot(category="沪深300")
        # 缓存中的快照树保持完整（下一次无过滤的调用仍拿到全市场）
        assert len(payload["groups"][0]["items"][0]["children"]) == 2

    def test_lite_detail(self, monkeypatch) -> None:
        payload = _snapshot_payload()
        row = _snapshot_row(payload, "2026-08-17T10:00:00")
        _read_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.intraday_dashboard_snapshot(detail="lite")
        assert result["ok"] is True
        assert result["detail"] == "lite"
        inst = result["groups"][0]["items"][0]["children"][0]["children"][0]
        assert "trend_history" not in inst


# ---------------------------------------------------------------------------
# 合并看板路由：dashboard_payload（MCP dashboard 工具的唯一实现）
# ---------------------------------------------------------------------------

class _FakeRouterDb(_FakeSnapshotDb):
    def __init__(self, snapshot: dict | None, max_bar: str = "2026-08-14") -> None:
        super().__init__(snapshot)
        self._max_bar = max_bar

    def get_market_dashboard_revision(self) -> tuple:
        return (f"{self._max_bar} 00:00:00", "", 1)


def _router_env(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    snapshot: dict | None,
    *,
    max_bar: str = "2026-08-14",
    trading_day: bool = True,
    past_open: bool = True,
) -> dict:
    """路由测试环境：可控日历/快照/日K版本，EOD 分支打桩为假 payload。"""
    monkeypatch.setattr(
        snapshot_module, "get_db", lambda: _FakeRouterDb(snapshot, max_bar)
    )
    monkeypatch.setattr(snapshot_module, "market_now", lambda: now)
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: trading_day)
    monkeypatch.setattr(snapshot_module, "is_past_market_open", lambda dt=None: past_open)
    monkeypatch.setattr(snapshot_module, "is_realtime_available", lambda dt=None: True)
    calls: dict = {}

    def fake_eod(detail: str = "full") -> dict:
        calls["eod_detail"] = detail
        return _snapshot_payload()

    monkeypatch.setattr(snapshot_module, "trend_dashboard_payload", fake_eod)
    return calls


class TestDashboardPayloadRouting:
    def test_auto_serves_snapshot_in_trading_hours(self, monkeypatch) -> None:
        row = _snapshot_row(_snapshot_payload(), "2026-08-17T10:00:00")
        calls = _router_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.dashboard_payload()
        assert result["ok"] is True
        assert result["data_mode"] == "intraday_snapshot"
        assert result["source"] == "snapshot"
        assert "eod_detail" not in calls  # 没碰 EOD 分支

    def test_auto_falls_back_to_eod_when_snapshot_missing(self, monkeypatch) -> None:
        """9:30~9:35 首轮未生成 / 任务停摆：auto 自动降级 EOD。"""
        calls = _router_env(monkeypatch, datetime(2026, 8, 17, 9, 32), None)
        result = snapshot_module.dashboard_payload()
        assert result["ok"] is True
        assert result["data_mode"] == "eod"
        assert calls["eod_detail"] == "full"

    def test_auto_eod_on_non_trading_day(self, monkeypatch) -> None:
        row = _snapshot_row(_snapshot_payload(), "2026-08-17T10:00:00")
        _router_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row, trading_day=False)
        result = snapshot_module.dashboard_payload()
        assert result["data_mode"] == "eod"

    def test_auto_eod_pre_open(self, monkeypatch) -> None:
        _router_env(monkeypatch, datetime(2026, 8, 17, 9, 0), None, past_open=False)
        result = snapshot_module.dashboard_payload()
        assert result["data_mode"] == "eod"

    def test_auto_eod_after_daily_bars_loaded(self, monkeypatch) -> None:
        """今日日K已落库（16:30 后）：EOD 含当日确认值，比快照权威。"""
        row = _snapshot_row(_snapshot_payload(), "2026-08-17T15:00:20")
        _router_env(
            monkeypatch, datetime(2026, 8, 17, 17, 0), row, max_bar="2026-08-17"
        )
        result = snapshot_module.dashboard_payload()
        assert result["data_mode"] == "eod"

    def test_mode_intraday_forces_snapshot(self, monkeypatch) -> None:
        row = _snapshot_row(_snapshot_payload(), "2026-08-17T10:00:00")
        _router_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.dashboard_payload(mode="intraday")
        assert result["ok"] is True
        assert result["data_mode"] == "intraday_snapshot"

    def test_mode_intraday_unavailable_returns_error(self, monkeypatch) -> None:
        _router_env(monkeypatch, datetime(2026, 8, 17, 9, 32), None)
        result = snapshot_module.dashboard_payload(mode="intraday")
        assert result["ok"] is False
        assert "尚未生成" in result["error"]

    def test_mode_eod_skips_snapshot_in_trading_hours(self, monkeypatch) -> None:
        row = _snapshot_row(_snapshot_payload(), "2026-08-17T10:00:00")
        _router_env(monkeypatch, datetime(2026, 8, 17, 10, 3), row)
        result = snapshot_module.dashboard_payload(mode="eod")
        assert result["ok"] is True
        assert result["data_mode"] == "eod"

    def test_invalid_mode_rejected(self, monkeypatch) -> None:
        _router_env(monkeypatch, datetime(2026, 8, 17, 10, 3), None)
        result = snapshot_module.dashboard_payload(mode="realtime")
        assert result["ok"] is False
        assert "mode" in result["error"]

    def test_eod_branch_category_filter(self, monkeypatch) -> None:
        _router_env(monkeypatch, datetime(2026, 8, 17, 9, 0), None, past_open=False)
        result = snapshot_module.dashboard_payload(category="沪深300", mode="eod")
        assert result["ok"] is True
        assert result["instrument_count"] == 1
        assert result["requested_category"] == "沪深300"
        no_match = snapshot_module.dashboard_payload(category="不存在", mode="eod")
        assert no_match["ok"] is False

    def test_eod_branch_lite(self, monkeypatch) -> None:
        _router_env(monkeypatch, datetime(2026, 8, 17, 9, 0), None, past_open=False)
        result = snapshot_module.dashboard_payload(detail="lite", mode="eod")
        assert result["ok"] is True
        assert result["detail"] == "lite"
        assert result["data_mode"] == "eod"
