"""Indicator precompute pipeline (P1.2).

Responsibilities:
- Full-symbol rebuild of the indicator caches from K-line history
  (vectorized, milliseconds per symbol). Full rebuild is deliberate:
  qfq adjustments retroactively rewrite history, so row-level
  incrementality is unsound (master plan D4).
- Default param-set registry with hash check (D3): a config or formula
  change marks the caches for a full rebuild.
- Dividend/adjustment detection (D9): raw 真源架构下行情不再被回溯改写，
  除权检测改为「vendor 除权因子 vs 本地因子表 diff」；变化的标的只需
  本地重物化 qfq（raw × 因子），无需重拉任何 K 线。
- Pre-rebuild backup (D10): VACUUM INTO snapshot before full rebuilds.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from audit.app_logger import get_logger
from core.indicators import INDICATOR_FORMULA_VERSION
from core.strategy_config import get_strategy_config
from core.trend import TREND_FORMULA_VERSION
from data.indicator_store import compute_indicator_frame, compute_trend_frame
from data.storage.db import get_db

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Param-set registry (D3)
# ---------------------------------------------------------------------------


def normalized_params_json(cfg: dict[str, Any]) -> str:
    """Deterministic serialization (sorted keys, fixed float repr via json)."""
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)


def params_hash(cfg: dict[str, Any]) -> str:
    digest = hashlib.sha1(
        (str(TREND_FORMULA_VERSION) + "|" + normalized_params_json(cfg)).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def default_param_set_needs_rebuild(cfg: dict[str, Any], db=None) -> bool:
    """True when the stored default param set no longer matches current config."""
    db = db or get_db()
    row = db.get_param_set("default")
    if row is None:
        return True
    current = normalized_params_json(cfg)
    return row["params_json"] != current or int(row["formula_version"]) != TREND_FORMULA_VERSION


def register_default_param_set(cfg: dict[str, Any], db=None) -> None:
    db = db or get_db()
    db.save_param_set("default", normalized_params_json(cfg), True, TREND_FORMULA_VERSION)


# ---------------------------------------------------------------------------
# Rebuild primitives
# ---------------------------------------------------------------------------


def rebuild_symbol(symbol: str, trend_cfg: dict, db=None) -> dict:
    """Full-symbol rebuild of both cache tables from stored K-lines."""
    db = db or get_db()
    symbol = str(symbol or "").strip().upper()
    df = db.load_market_data(symbol)
    if df.empty:
        return {"symbol": symbol, "status": "no_data", "rows": 0}
    ind_frame = compute_indicator_frame(df, trend_cfg)
    trend_frame = compute_trend_frame(df, trend_cfg)
    # 记录构建时的行情内容版本：之后 qfq 若被原位重写（除权重物化），
    # _cache_fresh 能据此识别缓存已陈旧，而不是只看日期。
    data_version = db.get_data_version(db.market_data_version_name(symbol))
    ind_rows = db.save_indicator_daily(symbol, ind_frame, INDICATOR_FORMULA_VERSION, data_version=data_version)
    trend_rows = db.save_trend_daily(symbol, trend_frame, TREND_FORMULA_VERSION, data_version=data_version)
    return {"symbol": symbol, "status": "rebuilt", "rows": ind_rows, "trend_rows": trend_rows}


def rebuild_all(symbols: list[str] | None = None, trend_cfg: dict | None = None, db=None) -> dict:
    db = db or get_db()
    trend_cfg = trend_cfg or get_strategy_config()
    if symbols is None:
        symbols = sorted(db.list_market_symbols())
    rebuilt, failed = 0, 0
    for symbol in symbols:
        try:
            result = rebuild_symbol(symbol, trend_cfg, db=db)
            if result["status"] == "rebuilt":
                rebuilt += 1
        except Exception:
            failed += 1
            logger.exception("Indicator rebuild failed for %s", symbol)
    register_default_param_set(trend_cfg, db=db)
    return {"total": len(symbols), "rebuilt": rebuilt, "failed": failed}


def rebuild_if_needed(db=None) -> dict:
    """Startup check: rebuild everything when params/formula drifted.

    Both formula versions are checked independently (D5): the trend param-set
    registry guards TREND_FORMULA_VERSION, and indicator_daily's own version
    column guards INDICATOR_FORMULA_VERSION (kimi review §2.3).
    """
    db = db or get_db()
    cfg = get_strategy_config()
    trend_stale = default_param_set_needs_rebuild(cfg, db=db)
    indicator_version = db.indicator_global_version()
    indicator_stale = indicator_version is None or int(indicator_version) != INDICATOR_FORMULA_VERSION
    if not trend_stale and not indicator_stale:
        return {"status": "up_to_date"}
    logger.info(
        "Formula/params changed (trend_stale=%s, indicator_stale=%s) — full indicator rebuild scheduled",
        trend_stale,
        indicator_stale,
    )
    db.backup_to()
    result = rebuild_all(trend_cfg=cfg, db=db)
    result["status"] = "rebuilt"
    return result


# ---------------------------------------------------------------------------
# Dividend / adjustment detection (D9)
# ---------------------------------------------------------------------------


def detect_adjustment_breaks(symbols: list[str], data_service, end_date: date | None = None) -> list[str]:
    """除权变更检测（因子 diff 法）：vendor 除权因子 vs 本地因子表。

    raw 真源架构下，除权不再意味着行情被回溯改写 —— 因子表变化即触发
    本地 qfq 重物化，无需重拉任何 K 线。新因子在此直接落库。
    返回因子发生变化的标的列表。
    """
    del end_date  # 旧比价法的参数，因子法不需要（lookback 常量已随死参数一并删除）
    try:
        _factors, changed = data_service.sync_ex_factors(symbols)
    except Exception:
        logger.exception("Ex-factor sync failed")
        return []
    for symbol in changed:
        logger.warning("Ex-factor change detected for %s — qfq rematerialization scheduled", symbol)
    return changed


def repair_broken_symbols(symbols: list[str], data_service, start_date: date | None = None, end_date: date | None = None) -> list[dict]:
    """除权变更修复：本地重物化 qfq（纯本地，秒级）。

    过渡期半迁移状态（raw 覆盖不如存量 qfq）下重物化会被拒绝，
    此时先全量补拉 raw 再物化 —— 只补 raw，不碰旧 qfq 逻辑。
    """
    results = []
    for symbol in symbols:
        try:
            result = data_service.rematerialize_qfq(symbol)
            if result.get("status") == "raw_incomplete" and end_date is not None:
                logger.warning("raw incomplete for %s; backfilling raw before rematerialize", symbol)
                result = data_service.backfill_daily_history(
                    symbol=symbol,
                    start_date=start_date or date(1990, 1, 1),
                    end_date=end_date,
                    adjust="none",
                )
            results.append(result)
            logger.info("Rematerialized qfq for %s after ex-factor change: %s", symbol, result.get("status"))
        except Exception:
            logger.exception("Failed to rematerialize qfq for %s", symbol)
    return results


def rebuild_after_backfill(symbols: list[str], db=None) -> dict:
    """Best-effort cache rebuild after instrument add/backfill jobs.

    Keeps caches fresh outside the daily 16:30 pipeline so dashboards never
    fall into the stale-cache fallback path after manual backfills.
    """
    db = db or get_db()
    try:
        trend_cfg = get_strategy_config()
        result = rebuild_all(symbols=symbols, trend_cfg=trend_cfg, db=db)
        logger.info("Post-backfill indicator rebuild for %s: %s", symbols, result)
        return result
    except Exception:
        logger.exception("Post-backfill indicator rebuild failed for %s", symbols)
        return {"status": "failed", "symbols": symbols}


def run_post_update_pipeline(settings, data_service, update_payload: dict, symbols: list[str], end_date: date, db=None) -> dict:
    """Post daily-update pipeline: dividend check → re-pull → indicator rebuild."""
    db = db or get_db()
    trend_cfg = get_strategy_config()
    symbols = [str(s).strip().upper() for s in symbols if s]
    if not symbols:
        symbols = sorted(db.list_market_symbols())

    broken = detect_adjustment_breaks(symbols, data_service, end_date)
    if broken:
        start_text = str(trend_cfg.get("backtest_start_primary", "2025-01-01"))
        start_date = date.fromisoformat(start_text)
        repair_broken_symbols(broken, data_service, start_date, end_date)

    updated = {
        str(r.get("symbol", "")).strip().upper()
        for r in update_payload.get("results", [])
        if isinstance(r, dict) and r.get("status") == "updated"
    }
    # qfq 自愈重物化（qfq_behind / 因子变化）的标的：行数与日期可能完全
    # 不变、status 仍是 up_to_date，但价格口径已改写，指标缓存必须重建。
    rematerialized = {
        str(r.get("symbol", "")).strip().upper()
        for r in update_payload.get("results", [])
        if isinstance(r, dict) and r.get("qfq_rematerialized") == "ok"
    }
    targets = sorted(updated | rematerialized | set(broken))
    if not targets:
        return {"status": "up_to_date", "dividend_breaks": broken, "rebuilt": 0}

    result = rebuild_all(symbols=targets, trend_cfg=trend_cfg, db=db)
    result["dividend_breaks"] = broken
    return result
