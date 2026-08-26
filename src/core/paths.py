"""项目根目录锚定（P2-13）：一切 cwd 相对路径的统一来源。

历史上 ``load_dotenv()``、``Path("data")``、``config/app.yaml``、``logs/app``
全部相对当前工作目录——非项目根启动（IDE / 其他 systemd 配置）会静默读写
错位置的 .env / DB / 日志。统一改为以 ``__file__`` 锚定项目根，可用
环境变量 ``TREND_QUANT_HOME`` 覆盖（如容器内挂载到其他位置）。
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    override = str(os.getenv("TREND_QUANT_HOME", "") or "").strip()
    if override:
        return Path(override).resolve()
    # src/core/paths.py → 项目根 = 上三级（src/core → src → 根）
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return project_root() / "data"


def logs_dir() -> Path:
    return project_root() / "logs"


def config_dir() -> Path:
    return project_root() / "config"


def web_dir() -> Path:
    return project_root() / "web"


def dotenv_path() -> Path:
    return project_root() / ".env"


def default_db_path() -> Path:
    return data_dir() / "trend_quant.db"
