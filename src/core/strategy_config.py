"""Canonical strategy/indicator parameters.

Single source of truth is the ``app_config`` DB table (key ``strategy``);
the code defaults below are the fallback and the seed for fresh databases.
Only live keys are kept here — parameters of the retired legacy engines
(momentum sections, entry thresholds, fee/slippage, lookback_days, ...)
were dropped during the storage consolidation.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

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


def get_strategy_config() -> dict[str, Any]:
    """Return the strategy config: DB value, lazily seeded from defaults.

    Falls back to the code defaults when the database is unavailable
    (bare test/script contexts).
    """
    from data.storage.db import get_db

    try:
        db = get_db()
        stored = db.get_config(_CONFIG_KEY, default=None)
        if isinstance(stored, dict):
            cfg = dict(DEFAULT_STRATEGY_CONFIG)
            cfg.update({k: v for k, v in stored.items() if k in _LIVE_KEYS})
            return cfg
        # Fresh database: seed it with the code defaults.
        db.set_config(_CONFIG_KEY, dict(DEFAULT_STRATEGY_CONFIG))
        return get_strategy_config()
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Strategy config unavailable in DB; using code defaults: %s", exc)
        return dict(DEFAULT_STRATEGY_CONFIG)
