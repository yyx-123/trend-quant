"""环境变量统一读取入口（P2-13 env 收口）。

散在各模块的 ``os.getenv``/``os.environ`` 直连全部收口到这里，环境变量
清单即本文件的函数列表：TICKFLOW_API_KEY / TICKFLOW_BASE_URL /
TICKFLOW_QUOTE_CACHE_TTL_SECONDS / TREND_MCP_INTRADAY_CACHE_TTL_SECONDS /
TREND_QUANT_LOG_DIR /
TREND_QUANT_DISABLE_SCHEDULER / TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD /
TREND_QUANT_PASSWORD_ITERATIONS / TREND_MCP_TOKENS /
TREND_MCP_ALLOWED_HOSTS / TREND_QUANT_HOME（core/paths 消费）。
"""

from __future__ import annotations

import os


def tickflow_api_key() -> str:
    """TickFlow 实时报价密钥（.env 配置，盘中/实时功能依赖）。"""
    return str(os.getenv("TICKFLOW_API_KEY", "") or "").strip()


def tickflow_base_url(default: str) -> str:
    """TickFlow 镜像站覆盖（默认 api_base_url 配置值）。"""
    return str(os.getenv("TICKFLOW_BASE_URL", "") or "").strip() or default


def quote_cache_ttl_seconds(default: float = 30.0) -> float:
    raw = str(os.getenv("TICKFLOW_QUOTE_CACHE_TTL_SECONDS", "") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def mcp_intraday_cache_ttl_seconds(default: float = 30.0) -> float:
    """MCP intraday_dashboard 看板 payload 缓存 TTL（秒）。

    与报价缓存窗口（默认同为 30s）对齐：TTL 内的重复看板请求直接复用
    上一次全量计算结果（单飞合并），30s 内的报价本来就会被报价层缓存。
    """
    raw = str(os.getenv("TREND_MCP_INTRADAY_CACHE_TTL_SECONDS", "") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def trend_log_dir() -> str:
    """日志目录覆盖（测试指向 logs/test）；空串表示用默认。"""
    return str(os.getenv("TREND_QUANT_LOG_DIR", "") or "").strip()


def scheduler_disabled() -> bool:
    return str(os.getenv("TREND_QUANT_DISABLE_SCHEDULER", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def bootstrap_admin_password() -> str:
    """内置管理员引导密码（仅首次创建生效）；空串表示用缺省。"""
    return str(os.getenv("TREND_QUANT_BOOTSTRAP_ADMIN_PASSWORD", "") or "").strip()


def password_iterations(default: int) -> int:
    """pbkdf2 迭代数（测试环境调低提速）；缺省用调用方给的生产默认。"""
    raw = str(os.getenv("TREND_QUANT_PASSWORD_ITERATIONS", "") or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def mcp_tokens_raw() -> str:
    """MCP Bearer token 配置原文（tokenA=user,tokenB=user2）。"""
    return str(os.getenv("TREND_MCP_TOKENS", "") or "")


def mcp_allowed_hosts_raw() -> str:
    """MCP DNS rebinding 保护的 allowed_hosts 配置原文（逗号分隔）。"""
    return str(os.getenv("TREND_MCP_ALLOWED_HOSTS", "") or "")
