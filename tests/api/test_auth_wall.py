"""登录墙（AuthWallMiddleware + /api/auth/*）行为测试。

覆盖：未登录拦截（页面 303 跳登录页 / API 401）、豁免路径、登录签发
cookie、退出销毁 session、登录页 next 参数。conftest 的 client fixture
已自动登录，本文件需要匿名请求时自建 TestClient。
"""

from __future__ import annotations


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

        resp = anon_client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert anon_client.post("/manual-trade/api/trades/list", json={}).status_code == 401

    def test_logout_get_not_allowed(self, anon_client, test_db) -> None:
        """GET 退出已废弃（CSRF 强制退出向量）：GET 不再路由到 logout。"""
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        resp = anon_client.get("/api/auth/logout", follow_redirects=False)
        assert resp.status_code == 405


class TestCsrfHeader:
    """P1-10：豁免名单外的 API 变更请求必须携带 X-Requested-With。"""

    def test_mutation_without_header_403(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        del anon_client.headers["X-Requested-With"]
        resp = anon_client.post("/manual-trade/api/trades/list", json={})
        assert resp.status_code == 403
        assert "X-Requested-With" in resp.json()["detail"]

    def test_mutation_with_header_passes_wall(self, anon_client, test_db) -> None:
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        assert anon_client.post("/manual-trade/api/trades/list", json={}).status_code == 200

    def test_login_endpoint_exempt_from_header(self, anon_client, test_db) -> None:
        """登录接口在豁免名单内：不带自定义头也可登录（login.html 无需改）。"""
        test_db.create_user("carol", "pw3")
        del anon_client.headers["X-Requested-With"]
        resp = anon_client.post(
            "/api/auth/login", json={"username": "carol", "password": "pw3"}
        )
        assert resp.status_code == 200

    def test_get_api_not_affected(self, anon_client, test_db) -> None:
        """GET API 不要求自定义头（CSRF 防线只针对变更方法）。"""
        test_db.create_user("carol", "pw3")
        anon_client.post("/api/auth/login", json={"username": "carol", "password": "pw3"})
        del anon_client.headers["X-Requested-With"]
        resp = anon_client.get("/subject-market/api/dashboard")
        assert resp.status_code == 200


class TestWallEdgeCases:
    def test_api_without_trailing_slash_is_api(self, anon_client) -> None:
        """/api（无尾斜杠）按 API 处理：401 JSON 而非 303 跳页。"""
        resp = anon_client.get("/api", follow_redirects=False)
        assert resp.status_code == 401

    def test_mcp_prefix_exact_segment_match(self, anon_client) -> None:
        """豁免前缀精确段匹配：/mcpanything 不豁免（页面请求 → 303 跳登录）。"""
        resp = anon_client.get("/mcpanything", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_mcp_requires_bearer_token(self, anon_client) -> None:
        """P0-1：/mcp 登录墙豁免但由 McpBearerMiddleware 把守——
        无 token 请求 /mcp/sse → 401（失败关闭，未配置 TREND_MCP_TOKENS 同样 401）。"""
        resp = anon_client.get("/mcp/sse", follow_redirects=False)
        assert resp.status_code == 401
        assert "detail" in resp.json()

    def test_mcp_invalid_token_401(self, anon_client) -> None:
        resp = anon_client.get(
            "/mcp/sse", headers={"Authorization": "Bearer wrong"}, follow_redirects=False
        )
        assert resp.status_code == 401


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
