"""标的大盘看板 API：快照优先返回 + 单例刷新触发。

GET /api/dashboard：当日盘中快照存在且日K未落库时返回快照，否则回退 EOD；
POST /api/dashboard/refresh：触发后台重算（非交易日等情形 skipped）。
"""

from __future__ import annotations

from datetime import date

import pytest

import app.routers.subject_market as subject_router
import services.dashboard_snapshot as snapshot_module


@pytest.fixture(autouse=True)
def _reset_snapshot_runner(monkeypatch: pytest.MonkeyPatch):
    """每个测试用独立的运行器实例，避免单例状态跨测试泄漏。"""
    runner = snapshot_module.IntradaySnapshotRunner()
    monkeypatch.setattr(subject_router, "snapshot_runner", runner)
    monkeypatch.setattr(snapshot_module, "snapshot_runner", runner)
    return runner


TODAY = date.today().isoformat()
SNAPSHOT_PAYLOAD = {
    "as_of": TODAY,
    "groups": [{"category_l1": "ETF", "items": []}],
    "is_intraday": True,
    "intraday_ts": f"{TODAY}T10:15:00",
}


def test_dashboard_prefers_today_snapshot(client, test_db):
    test_db.save_dashboard_snapshot("intraday", TODAY, SNAPSHOT_PAYLOAD)
    resp = client.get("/subject-market/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_intraday"] is True
    assert data["as_of"] == TODAY
    assert data["snapshot_ts"]  # 快照时间戳透出给前端


def test_dashboard_ignores_stale_snapshot(client, test_db):
    """快照不是当日的（如隔了几个交易日）时回退 EOD 看板。"""
    test_db.save_dashboard_snapshot("intraday", "2000-01-01", {**SNAPSHOT_PAYLOAD, "as_of": "2000-01-01"})
    resp = client.get("/subject-market/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert not data.get("is_intraday")
    assert "snapshot_ts" not in data


def test_dashboard_ignores_snapshot_when_eod_current(client, test_db, monkeypatch):
    """今日日K已落库（收盘补库完成）后以 EOD 确认值为准。"""
    test_db.save_dashboard_snapshot("intraday", TODAY, SNAPSHOT_PAYLOAD)
    monkeypatch.setattr(
        type(test_db),
        "get_market_dashboard_revision",
        lambda self: (f"{TODAY} 00:00:00", 100, "", 1),
    )
    resp = client.get("/subject-market/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert not data.get("is_intraday")


def test_refresh_skipped_on_non_trading_day(client, monkeypatch):
    monkeypatch.setattr(snapshot_module, "is_trading_day", lambda day: False)
    resp = client.post("/subject-market/api/dashboard/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"status": "skipped", "reason": "non_trading_day"}


def test_refresh_status_shape(client):
    resp = client.get("/subject-market/api/dashboard/refresh-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert "snapshot_ts" in data
