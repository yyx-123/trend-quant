"""Scheduled jobs for the application.

Currently only the daily market data update (16:30 on trading days),
migrated from the retired signal engine's ``run_daily_update``.
"""

from __future__ import annotations

from datetime import datetime

from audit.app_logger import get_logger
from core.benchmarks import benchmark_market_symbols
from core.calendar import is_trading_day, market_now
from core.ops_sentinel import clear_sentinel, write_sentinel
from core.settings import Settings
from core.strategy_config import get_strategy_config
from data.service import DataService, get_data_service
from data.storage.db import record_job_run_safely

logger = get_logger(__name__)


def _pool_symbols() -> list[str]:
    """Enabled instruments from the metadata table plus benchmark symbols, deduped."""
    import sqlite3

    from data.storage.db import get_db

    try:
        instruments = [
            item
            for item in get_db().list_instrument_metadata()
            if bool(item.get("enabled", True))
        ]
    except (RuntimeError, sqlite3.Error) as exc:
        logger.warning("Instrument metadata unavailable (%s); daily update will cover benchmarks only", exc)
        instruments = []  # database unavailable; fall back to benchmarks only

    symbols: list[str] = []
    seen: set[str] = set()
    for raw_symbol in [*(str(item.get("symbol")) for item in instruments), *benchmark_market_symbols()]:
        symbol = str(raw_symbol or "").strip().upper()
        if symbol == "" or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def daily_market_update_job(
    settings: Settings,
    data_service: DataService | None = None,
    force: bool = False,
) -> dict:
    """Incrementally backfill daily K-line data for the whole instrument pool.

    Records a ``job_runs`` row on non-trading days (skip) and on failures;
    successful trading-day runs are recorded by ``DataService.update_pool_daily``.

    ``force=True``（启动补偿调用）在非交易日也执行——定时任务本身只在
    工作日触发，但补跑可能发生在周末/节假日，用于补齐错过的交易日数据。
    """
    today = market_now().date()
    if not force and not is_trading_day(today):
        logger.info("Daily market update skipped: %s is not a trading day", today.isoformat())
        payload = {
            "ts": market_now().replace(tzinfo=None).isoformat(),
            "status": "skipped_non_trading_day",
            "results": [],
        }
        record_job_run_safely(
            "daily_update_skip",
            payload,
            run_date=today.isoformat(),
            status="skipped_non_trading_day",
        )
        return payload

    try:
        strategy_cfg = get_strategy_config()
        symbols = _pool_symbols()

        app_cfg = settings.app
        start_text = str(strategy_cfg.get("backtest_start_primary", "2015-01-01"))
        start_date = datetime.strptime(start_text, "%Y-%m-%d").date()

        service = data_service or get_data_service()
        payload = service.update_pool_daily(
            symbols=symbols,
            start_date=start_date,
            end_date=today,
            adjust=str(strategy_cfg.get("adjust", "qfq")),
            max_retries=max(int(app_cfg.daily_update_max_retries), 1),
            retry_interval_seconds=max(float(app_cfg.daily_update_retry_interval_seconds), 1.0),
        )
        # Post-update orchestration (dividend detection + indicator
        # rebuild) lives in app.main's update_job — core must not
        # depend on the services layer.
        payload["symbols"] = symbols
    except Exception as exc:
        # Surface the failure in job_runs instead of vanishing into the
        # scheduler log — the status bar must not keep showing a stale success.
        logger.exception("Daily market update job failed")
        record_job_run_safely(
            "daily_update",
            {"ts": market_now().replace(tzinfo=None).isoformat(), "error": str(exc)},
            run_date=today.isoformat(),
            status="failed",
        )
        # 失败哨兵（P2-22）：外部巡检（systemd/人工）无需打开页面即可发现失败
        write_sentinel("daily_update", str(exc))
        raise

    logger.info(
        "Daily market update finished: %s success, %s failed out of %s symbols",
        payload.get("success", 0),
        payload.get("failed", 0),
        payload.get("total", 0),
    )
    if int(payload.get("failed", 0) or 0) > 0:
        write_sentinel("daily_update", f"{payload.get('failed')} 只标的更新失败: {payload.get('failed_symbols')}")
    else:
        clear_sentinel("daily_update")
    return payload
