"""Canonical strategy/indicator parameters.

Single source of truth is the ``app_config`` DB table (key ``strategy``);
the code defaults below are the fallback and the seed for fresh databases.
Only live keys are kept here — parameters of the retired legacy engines
(momentum sections, entry thresholds, fee/slippage, lookback_days, ...)
were dropped during the storage consolidation.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from audit.app_logger import get_logger

logger = get_logger(__name__)

DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "adjust": "qfq",
    "n_short": 3,
    "n_mid": 5,
    "n_long": 8,
    "w_bias_short": 0.4,
    "w_bias_mid": 0.4,
    "w_bias_long": 0.2,
    "w_slope_short": 0.4,
    "w_slope_mid": 0.4,
    "w_slope_long": 0.2,
    "w_bias_norm": 0.5,
    "w_slope_norm": 0.5,
    "vol_ma_period": 20,
    "er_period": 10,
    "w_vol": 0.3,
    "w_er": 0.7,
    "atr_period": 20,
    "hard_stop_atr_mul_default": 1.5,
    "chandelier_stop_atr_mul": 2.5,
    "backtest_start_primary": "2025-01-01",
}

_LIVE_KEYS = frozenset(DEFAULT_STRATEGY_CONFIG)
_CONFIG_KEY = "strategy"

# 进程内短 TTL 缓存（P2-18）：此前被止损/看板/日更按次甚至按标的调用，
# 每次都是一次 DB 查询。30s TTL 足够覆盖配置变更的可见性；写路径经
# ``invalidate_strategy_config_cache`` 立即失效（进程外直写库最迟 TTL 后生效）。
import threading as _threading
import time as _time

_CACHE_TTL_SECONDS = 30.0
_cache_lock = _threading.Lock()
_cache_value: dict[str, Any] | None = None
_cache_expires_at = 0.0


def invalidate_strategy_config_cache() -> None:
    """策略配置写后调用：立即失效进程内缓存。

    当前应用内无策略配置写路径（配置经 SQL/脚本直改），进程外写最迟
    TTL（30s）后生效；测试夹具每用例调用本函数保证隔离（tests/conftest.py）。
    未来新增应用内写路径时必须调用。"""

    global _cache_value, _cache_expires_at
    with _cache_lock:
        _cache_value = None
        _cache_expires_at = 0.0


def get_strategy_config() -> dict[str, Any]:
    """Return the strategy config: DB value, lazily seeded from defaults.

    Falls back to the code defaults when the database is unavailable
    (bare test/script contexts).
    """
    global _cache_value, _cache_expires_at
    now = _time.monotonic()
    with _cache_lock:
        if _cache_value is not None and now < _cache_expires_at:
            return dict(_cache_value)

    from data.storage.db import get_db

    try:
        db = get_db()
        stored = db.get_config(_CONFIG_KEY, default=None)
        if isinstance(stored, dict):
            cfg = dict(DEFAULT_STRATEGY_CONFIG)
            cfg.update({k: v for k, v in stored.items() if k in _LIVE_KEYS})
        else:
            # Fresh database: seed it with the code defaults.
            cfg = dict(DEFAULT_STRATEGY_CONFIG)
            db.set_config(_CONFIG_KEY, dict(cfg))
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Strategy config unavailable in DB; using code defaults: %s", exc)
        return dict(DEFAULT_STRATEGY_CONFIG)

    with _cache_lock:
        _cache_value = dict(cfg)
        _cache_expires_at = now + _CACHE_TTL_SECONDS
    return cfg


def backfill_start_date() -> date:
    """回填起点统一口径（P2-9 附带项）：与日更任务同一配置
    （backtest_start_primary）。instrument_metadata.start_date 字段
    暂未被消费，统一以配置为准。"""
    text = str(get_strategy_config().get("backtest_start_primary", "2025-01-01"))
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return date(2025, 1, 1)
