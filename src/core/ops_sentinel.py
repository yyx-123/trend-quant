"""任务失败哨兵文件（P2-22）。

任务（日更等）失败时写 ``data/runtime/<job>.failed.json``，成功时清除：
- 外部巡检（systemd ExecCondition / 人工 ls）无需打开页面即可发现失败；
- 页面侧与导航栏盘后更新条闭环（同一 job_runs 数据源）。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.app_logger import get_logger
from core.calendar import market_now
from core.paths import data_dir

logger = get_logger(__name__)

_RUNTIME_DIR = data_dir() / "runtime"


def _sentinel_path(job_name: str) -> Path:
    return _RUNTIME_DIR / f"{job_name}.failed.json"


def write_sentinel(job_name: str, message: str) -> None:
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _sentinel_path(job_name).write_text(
            json.dumps(
                {
                    "job": job_name,
                    "failed_at": market_now().replace(tzinfo=None).isoformat(),
                    "message": str(message)[:2000],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to write sentinel for %s", job_name)


def clear_sentinel(job_name: str) -> None:
    try:
        _sentinel_path(job_name).unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to clear sentinel for %s", job_name)
