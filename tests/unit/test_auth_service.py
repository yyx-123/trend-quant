"""Unit tests for services.auth（登录墙 session：签发/校验/滑动续期/销毁）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services import auth


@pytest.fixture
def env(test_db):
    user = test_db.create_user("alice", "pw1")
    return test_db, user


class TestIssueResolve:
    def test_roundtrip(self, env) -> None:
        db, user = env
        token = auth.issue_session(user["id"], db=db)
        resolved, renewed = auth.resolve_session(token, db=db)
        assert resolved is not None
        assert resolved["username"] == "alice"
        assert resolved["is_admin"] is False
        # 刚签发的 session 剩余有效期充足，不触发续期
        assert renewed is False

    def test_unknown_token(self, env) -> None:
        db, _ = env
        assert auth.resolve_session("no-such-token", db=db) == (None, False)

    def test_empty_token(self, env) -> None:
        db, _ = env
        assert auth.resolve_session(None, db=db) == (None, False)
        assert auth.resolve_session("", db=db) == (None, False)


class TestExpiry:
    def test_expired_session_rejected_and_deleted(self, env) -> None:
        db, user = env
        db.create_session(user["id"], "tok-expired", datetime.now() - timedelta(seconds=1))
        assert auth.resolve_session("tok-expired", db=db) == (None, False)
        # 过期即清除，不留垃圾
        assert db.get_session_user("tok-expired") is None

    def test_sliding_renewal_extends_expiry(self, env) -> None:
        db, user = env
        # 剩余有效期不足一半阈值 → 触发续期
        soon = datetime.now() + auth.SESSION_TTL / 4
        db.create_session(user["id"], "tok-renew", soon)

        resolved, renewed = auth.resolve_session("tok-renew", db=db)
        assert resolved is not None
        assert renewed is True

        new_expires = datetime.strptime(
            db.get_session_user("tok-renew")["session_expires_at"], "%Y-%m-%d %H:%M:%S"
        )
        assert new_expires > datetime.now() + auth.SESSION_TTL / 2

    def test_no_renewal_when_plenty_left(self, env) -> None:
        db, user = env
        later = datetime.now() + auth.SESSION_TTL * 0.9
        db.create_session(user["id"], "tok-fresh", later)
        resolved, renewed = auth.resolve_session("tok-fresh", db=db)
        assert resolved is not None
        assert renewed is False


class TestDestroy:
    def test_destroy(self, env) -> None:
        db, user = env
        token = auth.issue_session(user["id"], db=db)
        auth.destroy_session(token, db=db)
        assert auth.resolve_session(token, db=db) == (None, False)

    def test_destroy_none_is_noop(self, env) -> None:
        db, user = env
        token = auth.issue_session(user["id"], db=db)
        auth.destroy_session(None, db=db)  # None 不应误删任何 session
        resolved, _ = auth.resolve_session(token, db=db)
        assert resolved is not None
        assert resolved["username"] == user["username"]

    def test_issue_cleans_expired_sessions(self, env) -> None:
        db, user = env
        db.create_session(user["id"], "tok-old", datetime.now() - timedelta(days=1))
        auth.issue_session(user["id"], db=db)
        assert db.get_session_user("tok-old") is None
