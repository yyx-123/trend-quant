"""API‑test layer fixtures.

Uses FastAPI's ``TestClient`` with a fully isolated app instance:
- Disables the APScheduler (``TREND_QUANT_DISABLE_SCHEDULER=1``).
- Overrides ``init_db`` and ``get_db`` to use the test database.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

# 测试日志独立目录：app.main 导入时会 setup_logging 并挂 RotatingFileHandler，
# 不覆盖的话 TestClient 的 httpx/uvicorn 日志会写进生产 logs/app/。
os.environ.setdefault("TREND_QUANT_LOG_DIR", "logs/test")

# 关键：在任何 monkeypatch 生效前完成 app → routers → services 的顶层导入。
# 服务模块顶层 `from data.storage.db import get_db` 是值绑定，会固化导入瞬间
# db_module.get_db 指向的对象；若首次导入发生在下方 isolate_api_db 的补丁窗口
# 内，服务模块会永久捕获某个测试的临时 lambda（其闭包指向该测试的 tmp 库），
# 后续所有测试的请求都会读写到第一个测试的临时库，造成跨测试污染。
import app.main  # noqa: F401


@pytest.fixture(autouse=True)
def _disable_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the background scheduler from starting during tests."""
    monkeypatch.setenv("TREND_QUANT_DISABLE_SCHEDULER", "1")


@pytest.fixture(autouse=True)
def _reset_login_guard() -> None:
    """每个用例重置登录限流状态：全测试会话共享 testclient IP，累计登录
    尝试不应触发 20 次/分钟的生产阈值。"""
    from services.login_guard import login_guard

    login_guard._ip_hits.clear()
    login_guard._fail_counts.clear()
    login_guard._locked_until.clear()


@pytest.fixture(autouse=True)
def isolate_api_db(test_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the FastAPI app use *test_db* instead of the production DB."""
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    # Also intercept init_db so lifespan() re-uses our test DB
    monkeypatch.setattr(db_module, "init_db", lambda db_path=None: test_db)


@pytest.fixture
def client(test_db) -> Generator[TestClient, None, None]:
    """Return a ``TestClient`` wired to the FastAPI app with test DB.

    全站登录墙（2026-08）后所有页面/API 都需要有效 session：这里统一创建
    tester 用户并完成登录，cookie 由 TestClient 自动携带，各测试文件无需
    关心鉴权。需要匿名（未登录）请求的登录墙测试请自建 TestClient，
    见 test_auth_wall.py。
    """
    from app.main import app

    test_db.create_user("tester", "pw-tester")
    with TestClient(app) as c:
        # 浏览器行为：app-common.js 的 fetch 拦截器为同源请求统一携带
        # X-Requested-With（AuthWall 的 CSRF 防线），测试客户端对齐。
        c.headers.update({"X-Requested-With": "XMLHttpRequest"})
        resp = c.post("/api/auth/login", json={"username": "tester", "password": "pw-tester"})
        assert resp.status_code == 200
        yield c


@pytest.fixture
def anon_client(test_db):
    """未登录的 TestClient（登录墙视角的匿名访客）。

    默认携带 X-Requested-With（对齐浏览器 app-common.js 的 fetch 拦截器）；
    需要验证 CSRF 头缺失行为的用例显式删该头。
    """
    from app.main import app

    with TestClient(app) as c:
        c.headers.update({"X-Requested-With": "XMLHttpRequest"})
        yield c


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.api)
