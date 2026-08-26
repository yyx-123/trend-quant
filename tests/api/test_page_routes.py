"""页面路由 smoke：全部页面 GET 200（防模板上下文缺失变量类回归，
如 stop_mode_toggle_title 未导入导致的 500）。"""

from __future__ import annotations

import pytest

PAGES = [
    "/rule-backtest",
    "/position-strategies",
    "/market-view",
    "/batch-backtest",
    "/subject-market",
    "/instruments",
    "/manual-trade",
]


@pytest.mark.parametrize("path", PAGES)
def test_page_route_renders(client, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    assert "<html" in resp.text



def test_root_redirects_to_subject_market(client) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/subject-market"
