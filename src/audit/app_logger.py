from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.env import trend_log_dir
from core.paths import logs_dir

LOG_DIR = logs_dir() / "app"
APP_LOG_PATH = LOG_DIR / "app.log"
ACCESS_LOG_PATH = LOG_DIR / "access.log"

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# app.log rotates at 10MB x 5 backups (~3 weeks at current volume).
APP_LOG_MAX_BYTES = 10 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 5
# access.log keeps the HTTP request timeline (uvicorn.access) in a separate
# file so request-level investigations do not require journalctl.
ACCESS_LOG_MAX_BYTES = 10 * 1024 * 1024
ACCESS_LOG_BACKUP_COUNT = 3


def _log_dir() -> Path:
    """日志目录：默认 logs/app，可用 TREND_QUANT_LOG_DIR 覆盖。

    测试进程（tests/api/conftest.py）指向 logs/test，避免 TestClient 的
    httpx/uvicorn 日志混进生产日志文件。注意在 setup_logging 调用时取值
    （而非模块导入时），保证 conftest 先设环境变量再 import app.main 生效。
    默认目录以 __file__ 锚定项目根（P2-13）。
    """
    override = trend_log_dir()
    return Path(override) if override else LOG_DIR


def setup_logging(level: str = "INFO") -> None:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_LOG_FORMAT,
        handlers=[
            RotatingFileHandler(
                log_dir / APP_LOG_PATH.name,
                maxBytes=APP_LOG_MAX_BYTES,
                backupCount=APP_LOG_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )
    _attach_access_log_handler(log_dir)


def _attach_access_log_handler(log_dir: Path) -> None:
    # uvicorn.access uses its own stdout handler with propagate=False, so the
    # request timeline never reaches the root handlers; attach a dedicated
    # rotating file handler to persist it to logs/app/access.log.
    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(h, RotatingFileHandler) for h in access_logger.handlers):
        return
    handler = RotatingFileHandler(
        log_dir / ACCESS_LOG_PATH.name,
        maxBytes=ACCESS_LOG_MAX_BYTES,
        backupCount=ACCESS_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    access_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
