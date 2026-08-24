"""登录墙（AuthWallMiddleware + /api/auth/*）行为测试。

覆盖：未登录拦截（页面 303 跳登录页 / API 401）、豁免路径、登录签发
cookie、退出销毁 session、登录页 next 参数。conftest 的 client fixture
已自动登录，本文件需要匿名请求时自建 TestClient。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def anon_client(test_db):
    """未登录的 TestClient（登录墙视角的匿名访客）。"""
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestWallBlocksAnonymous:
    def test_page_redirects_to_login_with_next(self, anon_client) -> None:
        resp = anon_client.get("/manual-trade", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=%2Fmanual-trade"

    def test_root_redirects_to_login(self, anon_client) -> None:
        resp = anon_client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")

    def test_page_query_preserved_in_next(self, anon_client) -> None:
        resp = anon_client.get("/market-view?symbol=510300.SS", follow_redirects=False)
        assert resp.status_code == 303
        assert "next=" in resp.headers["location"]
        assert "market-view" in resp.headers["location"]

    def test_api_returns_401_json(self, anon_client) -> None:
        resp = anon_client.post("/manual-trade/api/trades/list", json={})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "未登录或登录已过期"

    def test_get_api_returns_401(self, anon_client) -> None:
        resp = anon_client.get("/subject-market/api/dashboard")
        assert resp.status_code == 401


class TestExemptPaths:
    def test_login_page_accessible(self, anon_client) -> None:
        resp = anon_client.get("/login")
        assert resp.status_code == 200
        assert "趋势 ETF 系统" in resp.text

    def test_static_accessible(self, anon_client) -> None:
        resp = anon_client.get("/static/style.css")
        assert resp.status_code == 200


class TestLoginLogout:
    def test_login_sets_cookie_and_grants_access(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        resp = anon_client.post(
            "/api/auth/login", json={"username": "carol", "password": "pw3"}
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "carol"
        assert "tq_session" in resp.cookies

        # cookie 由 TestClient 自动携带：页面与 API 均放行
        assert anon_client.get("/manual-trade", follow_redirects=False).status_code == 200
        assert anon_client.post("/manual-trade/api/trades/list", json={}).status_code == 200

    def test_login_wrong_password_401(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        resp = anon_client.post(
            "/api/auth/login", json={"username": "carol", "password": "bad"}
        )
        assert resp.status_code == 401
        assert "tq_session" not in resp.cookies

    def test_login_page_redirects_when_already_authed(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        resp = anon_client.get("/login", follow_redirects=False)
        assert resp.status_code == 303

    def test_logout_destroys_session(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        assert anon_client.post("/manual-trade/api/trades/list", json={}).status_code == 200

        resp = anon_client.get("/api/auth/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert anon_client.post("/manual-trade/api/trades/list", json={}).status_code == 401

    def test_me_returns_current_user(self, client) -> None:
        # client fixture 已自动登录 tester
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "tester"


class TestSessionModel:
    def test_password_stored_hashed(self, test_db) -> None:
        test_db.create_user("dave", "pw4")
        stored = test_db.get_user_by_username("dave")["password"]
        assert stored.startswith("pbkdf2_sha256$")
        assert "pw4" not in stored

    def test_plaintext_password_migrated_on_init(self, test_db) -> None:
        """历史明文密码在 _migrate_schema 中被改写为 pbkdf2 哈希，且可正常登录。"""
        from services import trade_records as tr

        with test_db._connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password) VALUES ('legacy', 'plainpw')"
            )
        test_db._migrate_schema()

        stored = test_db.get_user_by_username("legacy")["password"]
        assert stored.startswith("pbkdf2_sha256$")
        assert tr.authenticate("legacy", "plainpw", db=test_db)["username"] == "legacy"
