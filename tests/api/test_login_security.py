"""P1-1/P1-3 的 API 层行为测试：登录限流锁定端到端 + 内置管理员 ensure。"""

from __future__ import annotations

import sqlite3

import pytest

from services.login_guard import FAIL_MAX_CONSECUTIVE


class TestLoginEndpointGuard:
    """登录路由接入限流/锁定的端到端行为（经 API 层）。"""

    def test_lock_after_consecutive_failures(self, anon_client, test_db) -> None:
        test_db.create_user("eve", "right-pw")
        for _ in range(FAIL_MAX_CONSECUTIVE):
            resp = anon_client.post(
                "/api/auth/login", json={"username": "eve", "password": "bad"}
            )
            assert resp.status_code == 401
        # 锁定后即使密码正确也 429
        resp = anon_client.post(
            "/api/auth/login", json={"username": "eve", "password": "right-pw"}
        )
        assert resp.status_code == 429

    def test_success_clears_fail_count(self, anon_client, test_db) -> None:
        test_db.create_user("eve", "right-pw")
        for _ in range(FAIL_MAX_CONSECUTIVE - 1):
            anon_client.post("/api/auth/login", json={"username": "eve", "password": "bad"})
        assert (
            anon_client.post(
                "/api/auth/login", json={"username": "eve", "password": "right-pw"}
            ).status_code
            == 200
        )


class TestBuiltinAdmin:
    """P1-3：lifespan ensure 内置管理员 yyx。"""

    def test_builtin_admin_created_on_startup(self, anon_client, test_db) -> None:
        user = test_db.get_user_by_username("yyx")
        assert user is not None
        assert user["is_admin"] is True
        # 默认引导密码可登录（TestClient 进入 lifespan 即 ensure）
        resp = anon_client.post(
            "/api/auth/login", json={"username": "yyx", "password": "20160702"}
        )
        assert resp.status_code == 200

    def test_existing_user_password_not_reset(self, test_db, monkeypatch) -> None:
        """已存在的 yyx 不强制重置密码，仅补 is_admin。"""
        from app.main import _ensure_builtin_admin

        test_db.create_user("yyx", "custom-pw")
        _ensure_builtin_admin(test_db)
        user = test_db.get_user_by_username("yyx")
        assert user["is_admin"] is True
        from services import trade_records as tr

        assert tr.authenticate("yyx", "custom-pw", db=test_db)["username"] == "yyx"


class TestBackupAndSqliteHardening:
    """P1-2/P2-23：backup_to 加固 + busy_timeout/foreign_keys。"""

    def test_backup_keep_one_prunes(self, test_db) -> None:
        first = test_db.backup_to(keep=1)
        second = test_db.backup_to(keep=1)
        assert second.exists()
        assert not first.exists()
        backups = list((test_db.db_path.parent / "backups").glob("trend_quant-*.db"))
        assert backups == [second]

    def test_backup_rejects_single_quote_path(self, test_db, tmp_path) -> None:
        with pytest.raises(ValueError, match="single quote"):
            test_db.backup_to(backup_dir=tmp_path / "it's-bad")

    def test_foreign_keys_enforced(self, test_db) -> None:
        with test_db._connect() as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions (user_id, token, expires_at)"
                    " VALUES (999999, 't', '2099-01-01 00:00:00')"
                )

    def test_connect_timeout_set(self, test_db) -> None:
        with test_db._connect() as conn:
            # busy_timeout 生效（timeout=30 → 30000ms）
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
