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
def isolate_api_db(test_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the FastAPI app use *test_db* instead of the production DB."""
    import data.storage.db as db_module

    monkeypatch.setattr(db_module, "get_db", lambda: test_db)
    monkeypatch.setattr(db_module, "_db_instance", test_db)
    # Also intercept init_db so lifespan() re-uses our test DB
    monkeypatch.setattr(db_module, "init_db", lambda db_path=None: test_db)


@pytest.fixture
def client(test_db) -> Generator[TestClient, None, None]:
    """Return a ``TestClient`` wired to the FastAPI app with test DB."""
    from app.main import app

    with TestClient(app) as c:
        yield c


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.api)
