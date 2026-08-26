"""脚本公共件（P2-13）：.env 加载 / DB_PATH / TickFlow 客户端构造统一来源。

所有 scripts 经 ``from _common import ...`` 使用（scripts 已在 sys.path）。
- 直接运行脚本时 .env 也能加载（此前 sync_stock_industry /
  fetch_etf_holdings / import_all_etf_constituents 不加载 .env，
  名称字段静默为空）；
- DB_PATH 以 __file__ 锚定项目根，从任意 cwd 运行都指向同一生产库；
- TickFlow 客户端构造从 3 套复制收敛为一处。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv

from core.paths import default_db_path, dotenv_path

# 已存在的环境变量优先（不覆盖）
load_dotenv(dotenv_path())

DB_PATH = default_db_path()


def setup_script_logging(name: str = "scripts"):
    """脚本日志：控制台 + logs/ops/scripts.log（P2-21：季度窗口任务的执行
    历史不再只靠终端回显；service 层 logger 输出也随 basicConfig 落文件）。"""
    import logging

    from core.paths import logs_dir

    log_dir = logs_dir() / "ops"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "scripts.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(name)


def make_tickflow_client():
    """构造 TickFlow 客户端（API key/base_url 走 core.env 统一入口）。"""
    from tickflow import TickFlow

    from core.env import tickflow_api_key, tickflow_base_url
    from core.settings import load_settings

    return TickFlow(
        api_key=tickflow_api_key(),
        base_url=tickflow_base_url(load_settings().tickflow.api_base_url),
    )
