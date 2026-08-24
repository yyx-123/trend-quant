"""标的大盘看板 API：快照优先返回 + 定时快照状态轮询。

GET /api/dashboard：当日盘中快照存在且日K未落库时返回快照，否则回退 EOD；
GET /api/dashboard/refresh-status：定时快照任务进度（快照只由定时任务更新，
页面不触发重算）。
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
# 生产上快照 as_of 是完整时间戳（intraday_service 里 datetime.now().isoformat()），
# 看板接口须按日期部分判断「当日快照」。
SNAPSHOT_AS_OF = f"{TODAY}T10:15:19"
SNAPSHOT_PAYLOAD = {
    "as_of": SNAPSHOT_AS_OF,
    "groups": [{"category_l1": "ETF", "items": []}],
    "is_intraday": True,
    "intraday_ts": f"{TODAY}T10:15:00",
}


def test_dashboard_prefers_today_snapshot(client, test_db):
    test_db.save_dashboard_snapshot("intraday", SNAPSHOT_AS_OF, SNAPSHOT_PAYLOAD)
    resp = client.get("/subject-market/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_intraday"] is True
    assert data["as_of"] == SNAPSHOT_AS_OF
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
    test_db.save_dashboard_snapshot("intraday", SNAPSHOT_AS_OF, SNAPSHOT_PAYLOAD)
    monkeypatch.setattr(
        type(test_db),
        "get_market_dashboard_revision",
        lambda self: (f"{TODAY} 00:00:00", 100, "", 1),
    )
    resp = client.get("/subject-market/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert not data.get("is_intraday")


def test_refresh_status_shape(client):
    resp = client.get("/subject-market/api/dashboard/refresh-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert "snapshot_ts" in data
