"""Scheduled jobs for the application.

- ``daily_market_update_job``: the 16:30 post-close update (daily K + the
  weekly/monthly sync), migrated from the retired signal engine's
  ``run_daily_update``;
- ``intraday_period_refresh_job``: every 5 minutes during the session, refresh
  ONLY the weekly/monthly in-progress bars (daily K is deliberately untouched —
  it stays EOD-only, with the view-only intraday overlay for charts).
"""

from __future__ import annotations

import threading
from datetime import datetime

from audit.app_logger import get_logger
from core import run_freeze
from core.bars import PERIOD_MONTHLY, PERIOD_WEEKLY
from core.benchmarks import benchmark_market_symbols
from core.calendar import is_realtime_available, is_trading_day, market_now
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


def _sync_period_bars(service: DataService, symbols: list[str], today) -> dict:
    """周K/月K 同步（日更任务的一部分，失败不影响日K结果）。

    job_runs 里只留汇总，逐标的明细（400+ 行 × 周期数）不塞进日更 payload：
    日更行本身已记录全量 results，再叠加一份会让 status 接口的 payload 成倍膨胀。
    """
    try:
        period_payload = service.update_pool_periods(symbols, end_date=today)
    except Exception as exc:
        logger.exception("Period (weekly/monthly) market update failed")
        return {"error": str(exc)}
    return {
        period: {key: value for key, value in info.items() if key != "results"}
        for period, info in (period_payload.get("periods") or {}).items()
    }


def intraday_period_refresh_job(
    settings: Settings,
    data_service: DataService | None = None,
) -> dict:
    """盘中每 5 分钟刷新周/月K 的当期（未收盘）bar —— **日K刻意不动**。

    为什么只刷周/月：周/月的当期 bar 是「本周期至今」的滚动值，盘后一天才更新
    一次意味着盘中看到的周/月线最多滞后一个交易日；而日K的盘中新鲜度走的是
    另一条路（``data.intraday_service`` 的 view-only 合成 bar），日K库仍只由
    16:30 的日更写入收盘数据 —— 这条硬约束不因本任务改变。

    与 16:30 那轮的分工：
    - 本任务 ``sync_factors=False``（因子盘中几乎不变，不白打请求）、不记
      ``job_runs``（一天约 48 轮，记了只是噪声）；
    - 16:30 那轮照旧带因子同步与 job_runs，并把当期 bar 收成定值。

    时段门控用 ``is_realtime_available``（交易日 + 9:30~15:00，含午休——午休时
    报价仍反映上午收盘状态）；非交易时段直接跳过，不产生任何请求。
    """
    if not is_realtime_available():
        return {"status": "skipped_outside_session"}

    today = market_now().date()
    try:
        symbols = _pool_symbols()
        service = data_service or get_data_service()
        payload = service.update_pool_periods(
            symbols,
            (PERIOD_WEEKLY, PERIOD_MONTHLY),
            end_date=today,
            sync_factors=False,
            job_type="",  # 不污染 job_runs
        )
    except Exception as exc:
        # 盘中刷新失败不写哨兵：下一轮（5 分钟后）会自愈，不值得报警；
        # 16:30 那轮才是权威，失败才需要外显。
        logger.exception("Intraday weekly/monthly refresh failed")
        return {"status": "error", "error": str(exc)}

    summary = {
        period: {
            "updated": info.get("updated"),
            "up_to_date": info.get("up_to_date"),
            "failed": info.get("failed"),
        }
        for period, info in (payload.get("periods") or {}).items()
    }
    if payload.get("failed"):
        logger.warning("Intraday weekly/monthly refresh had failures: %s", summary)
    else:
        logger.info("Intraday weekly/monthly refresh done: %s", summary)
    return {"status": payload.get("status"), "periods": summary, "ts": payload.get("ts")}


def _spawn_same_day_catchup(
    settings: Settings, data_service: DataService | None, *, force: bool, today,
    after_update=None,
) -> None:
    """冻结顺延后的当日一次性补跑哨兵（GLM53F-P2-13）。

    顺延到"下一次定时触发"意味着当日 EOD/除权检测/指标重建全停一天——
    改为挂一个 daemon 线程：解冻后立刻补跑一次（最长再等 2 小时，仍冻结
    则放弃，由次日 cron/启动补偿兜底）。幂等：单例判断在锁内完成
    （check-then-act 竞态会让两个超时顺延源各起一个
    哨兵）；补跑本体经 daily_market_update_job 的模块级单飞锁，与定时/
    启动补偿互斥（同评审：哨兵此前绕过 main.py 闭包内的 _update_job_lock）。
    """
    # threading 用**模块级引用**（函数体内 import 会让测试的
    # monkeypatch(jobs.threading) 永不生效 → 钉子实际起真线程并与断言竞态）
    global _catchup_sentinel
    with _catchup_spawn_lock:
        if _catchup_sentinel is not None and _catchup_sentinel.is_alive():
            return

        def _watch() -> None:
            import time as _time

            # 预算内的"等解冻 → 补跑"循环。必须循环而非一次性（实证）：
            # 哨兵在解冻与日更的"检查再检查"之间可能被别的 run 重新冻结，此时
            # 日更会再次顺延——旧写法直接调 after_update，会在**数据还没落地**
            # 的情况下跑一次 pipeline（symbols=[] 的全池扫描），而当日 rebuild
            # 再也不会发生。
            remaining = 7200
            while remaining > 0:
                while run_freeze.is_frozen() and remaining > 0:
                    _time.sleep(60)
                    remaining -= 60
                if run_freeze.is_frozen():
                    logger.warning("same-day catchup abandoned: still frozen after 2h")
                    return
                if market_now().date() != today:
                    return  # 跨日了，交给当日 cron/启动补偿
                # 预算按**轮次**扣减（此前只在 sleep 时扣减，
                # 若作业返回 deferred 而冻结已解除，会变成不 sleep 的忙循环，
                # 预算永不递减、线程不退出——实测 10 秒内 15532 次调用）。
                remaining -= 60
                logger.info("same-day catchup: unfrozen, running daily update now")
                try:
                    payload = daily_market_update_job(settings, data_service, force=force)
                except Exception:
                    logger.exception("same-day catchup failed")
                    return
                status = str(payload.get("status") or "")
                if status == "deferred_backtest_running":
                    # 又被冻结：回到等待（预算继续消耗），不跑 pipeline；
                    # 若此时并未冻结（状态语义异常/竞态），退避一轮避免热转
                    logger.info("same-day catchup: re-deferred, waiting again")
                    if not run_freeze.is_frozen():
                        _time.sleep(60)
                    continue
                if status in ("skipped_already_running", "skipped_non_trading_day"):
                    # 另一触发源正在/已经完成日更：pipeline 由那一侧负责，
                    # 本哨兵不得叠加（实证：旧写法会跑第二遍）
                    logger.info("same-day catchup: %s — pipeline owned by the other trigger", status)
                    return
                # 补跑 = 数据 + post-update pipeline（除权检测 + 指标重建）。
                # pipeline 的编排在 app/main.py
                # （core 不得 import services），此前哨兵只调日更本体 → 顺延日
                # 的 indicator_daily/trend_daily 整日缺失。回调由调用方注入。
                if after_update is not None:
                    try:
                        after_update(payload)
                    except Exception:
                        logger.exception("same-day catchup post-update pipeline failed")
                return
            logger.warning("same-day catchup abandoned: budget exhausted")

        _catchup_sentinel = threading.Thread(
            target=_watch, daemon=True, name="daily-update-catchup"
        )
        _catchup_sentinel.start()


_catchup_sentinel = None
# 日更单飞锁：从 main.py 闭包下沉到模块级——
# 定时触发 / 启动补偿 / 冻结补跑哨兵三条路径共用，任何时刻至多一个
# daily_market_update_job 在执行；占用者立即返回，不排队。
_DAILY_UPDATE_LOCK = threading.Lock()
_catchup_spawn_lock = threading.Lock()


def daily_market_update_job(
    settings: Settings,
    data_service: DataService | None = None,
    force: bool = False,
    after_update=None,
) -> dict:
    """Incrementally backfill daily K-line data for the whole instrument pool.

    Records a ``job_runs`` row on non-trading days (skip) and on failures;
    successful trading-day runs are recorded by ``DataService.update_pool_daily``.

    ``force=True``（启动补偿调用）在非交易日也执行——定时任务本身只在
    工作日触发，但补跑可能发生在周末/节假日，用于补齐错过的交易日数据。

    决策 A3（运行期数据冻结）：回测 run 执行期间冻结写任务——run 优先、
    日更等待（最长 30 分钟轮询解冻；超时则顺延到下一次定时触发）。
    单飞：模块级锁自守，与调用方（定时/补偿/哨兵）
    解耦——哨兵不再绕过 main.py 闭包内的私有锁。非交易日判断前移
    节假日不必为永远轮不到的解冻白等 30 分钟。
    """
    if not _DAILY_UPDATE_LOCK.acquire(blocking=False):
        logger.info("Daily market data update already running; skipping duplicate trigger")
        return {
            "ts": market_now().replace(tzinfo=None).isoformat(),
            "status": "skipped_already_running",
            "results": [],
        }
    try:
        return _daily_market_update_job_locked(
            settings, data_service, force=force, after_update=after_update
        )
    finally:
        _DAILY_UPDATE_LOCK.release()


def _daily_market_update_job_locked(
    settings: Settings,
    data_service: DataService | None,
    *,
    force: bool,
    after_update=None,
) -> dict:
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

    # 冻结门对 force 同样生效（评审 DS-P2-6：启动补偿也不能抢跑写任务——
    # 否则补偿与活跃 run 并发读两版数据；无活跃 run 时冻结非真，照样放行）
    if run_freeze.is_frozen():
        import time as _time

        waited = 0
        while run_freeze.is_frozen() and waited < 1800:
            logger.info("daily update deferred: %d backtest run(s) active, waiting...",
                        run_freeze.active_count())
            _time.sleep(30)
            waited += 30
        if run_freeze.is_frozen():
            logger.warning("daily update deferred: backtest still running after 30min")
            # 顺延必须留痕（评审 DS-P2-6）：job_runs 记录，日更推迟可见
            payload = {
                "ts": market_now().replace(tzinfo=None).isoformat(),
                "status": "deferred_backtest_running",
                "results": [],
            }
            record_job_run_safely(
                "daily_update_deferred", payload,
                run_date=today.isoformat(), status="deferred_backtest_running",
            )
            # GLM53F-P2-13：顺延 ≠ 饿一整天——挂当日一次性补跑哨兵
            _spawn_same_day_catchup(
                settings, data_service, force=force, today=today,
                after_update=after_update,
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
        # 周K/月K 随同一次盘后任务补齐（自身的成败单独记 job_runs，
        # 不并入日K的成功/失败计数，行情表互相不拖累）。
        payload["periods"] = _sync_period_bars(service, symbols, today)
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


# ----------------------------------------------------------------------
# 实盘运行器（投研基建 L3 薄版，详设 §5.7）：交易日 14:00 目标持仓清单
# ----------------------------------------------------------------------


def live_daily_list_job(settings: Settings) -> dict:
    """交易日 14:00 前后：产出已部署策略的目标持仓清单 + 对账昨日清单。

    未部署策略（app_config 缺 portfolio.live_strategy_version_id）时跳过——
    这是配置驱动的可选项，不是错误。对账用前一交易日清单。
    """
    from core.calendar import previous_trading_day
    from gateway.live_overlay import default_live_overlay
    from portfolio.live import generate_daily_list, reconcile_daily_list

    now = market_now()
    if not is_trading_day(now.date()):
        return {"status": "skipped_non_trading_day"}

    from data.storage.db import get_db

    db = get_db()
    strategy_version_id = db.get_config("portfolio.live_strategy_version_id")
    if not strategy_version_id:
        return {"status": "skipped_no_deployed_strategy"}
    try:
        # 配置解析此前在 try 之外——配置畸形（如非数字的
        # live_initial_capital）会直接抛穿，job_runs 无任何留痕。
        user_id = int(db.get_config("portfolio.live_user_id", 1))
        initial_capital = float(db.get_config("portfolio.live_initial_capital", 1_000_000))
        target = generate_daily_list(
            db, strategy_version_id=strategy_version_id, user_id=user_id,
            as_of=now, initial_capital=initial_capital,
            live_overlay=default_live_overlay(db),
        )
        prev_day = previous_trading_day(now.date())
        reconcile_daily_list(
            db, list_date=prev_day.isoformat(),
            strategy_version_id=strategy_version_id, user_id=user_id,
        )
        payload = {
            "status": "ok",
            "list_date": now.date().isoformat(),
            "buys": len(target.get("buys", [])),
            "sells": len(target.get("sells", [])),
        }
        record_job_run_safely("live_daily_list", payload, run_date=now.date().isoformat(), status="ok")
        return payload
    except Exception as exc:
        logger.exception("live daily list failed")
        record_job_run_safely(
            "live_daily_list", {"error": str(exc)},
            run_date=now.date().isoformat(), status="failed",
        )
        return {"status": "failed", "error": str(exc)}
