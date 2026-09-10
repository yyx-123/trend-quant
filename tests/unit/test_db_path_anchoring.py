"""init_db 的默认库路径必须锚定项目根并支持 TREND_QUANT_HOME。

回归背景：``init_db`` 的默认参数曾是硬编码的 CWD 相对字符串
``"data/trend_quant.db"``，与 ``core/paths``「已消除 CWD 相对路径」的文档
相矛盾。后果是 ``TREND_QUANT_HOME`` 对主应用（``app.main`` 的无参
``init_db()``）完全失效——从非项目根目录启动会静默连到另一个库，
容器/多环境部署时尤其危险。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from core import paths
from data.storage import db as db_module


@pytest.fixture(autouse=True)
def _restore_singleton():
    """init_db 会改写进程级单例，用例前后都要还原，避免污染其他测试。"""
    db_module.reset_db_instance_for_tests()
    yield
    db_module.reset_db_instance_for_tests()


def test_default_db_path_follows_project_root() -> None:
    assert paths.default_db_path() == paths.data_dir() / "trend_quant.db"


def test_init_db_default_is_not_cwd_relative(tmp_path, monkeypatch) -> None:
    """CWD 指向别处时，无参 init_db() 仍必须解析到项目根下的库。"""
    monkeypatch.chdir(tmp_path)
    db = db_module.init_db()
    assert db.db_path == paths.default_db_path()
    assert db.db_path.is_absolute()
    assert tmp_path not in db.db_path.parents


def test_init_db_honours_trend_quant_home(tmp_path, monkeypatch) -> None:
    """TREND_QUANT_HOME 覆盖必须对 app.main 走的无参路径同样生效。"""
    monkeypatch.setenv("TREND_QUANT_HOME", str(tmp_path))
    db = db_module.init_db()
    assert db.db_path == tmp_path / "data" / "trend_quant.db"


def test_init_db_explicit_path_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TREND_QUANT_HOME", str(tmp_path / "ignored"))
    target = tmp_path / "explicit.db"
    db = db_module.init_db(target)
    assert db.db_path == target
