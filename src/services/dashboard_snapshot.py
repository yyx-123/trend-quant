"""标的大盘盘中实时看板：快照单例运行器。

盘中实时计算耗时长（全市场实时报价 + 逐标的指标重算，约 1 分钟），
因此采用「快照 + 后台单例重算」模式：

- 交易时段内的定时任务（core/scheduler 里 9 个时点）和用户打开看板页面
  都会通过 ``ensure_running`` 触发重算；同一进程任意时刻只允许一个在跑
  的实时计算任务，重复触发直接复用进行中的任务。
- 每次计算完成，结果作为最新快照持久化到独立的 ``dashboard_snapshot``
  表（单行替换），页面打开时优先展示该快照。

红线：快照 payload 只服务于看板展示，严禁写入 market_data_* 日K库 ——
日K数据只能由收盘后的补库任务写入稳定值。``build_intraday_dashboard``
本身全程在内存中合成当日K线，不落库；本模块的持久化也仅限快照表。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.calendar import is_past_market_open, is_trading_day, market_now
from core.display import filter_fully_classified
from data.intraday_service import build_intraday_dashboard
from data.service import DataService
from data.storage.db import get_db
from services.market_indicators import trend_config

logger = logging.getLogger(__name__)


class IntradaySnapshotRunner:
    """进程级单例：管理盘中看板重算任务与最新快照。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._percent = 0.0
        self._message = ""
        self._last_error: str | None = None
        self._last_finished_at: str | None = None
        self._snapshot: dict | None = None
        self._snapshot_loaded = False

    # ------------------------------------------------------------------
    # 快照读取（内存优先，首次访问时从 DB 懒加载，进程重启后仍可展示）
    # ------------------------------------------------------------------
    def latest_snapshot(self) -> dict | None:
        """Return the latest snapshot dict (kind/as_of/computed_at/payload)."""
        with self._lock:
            if not self._snapshot_loaded:
                self._snapshot_loaded = True
                loaded: dict | None = None
                try:
                    loaded = get_db().load_dashboard_snapshot()
                except Exception:
                    logger.exception("Failed to load dashboard snapshot from DB")
                self._snapshot = loaded
            return self._snapshot

    # ------------------------------------------------------------------
    # 状态查询（前端轮询用）
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "percent": self._percent,
                "message": self._message,
                "last_error": self._last_error,
                "last_finished_at": self._last_finished_at,
                "snapshot_ts": (self._snapshot or {}).get("computed_at"),
            }

    # ------------------------------------------------------------------
    # 触发重算（定时任务与页面打开共用同一入口，保证单例）
    # ------------------------------------------------------------------
    def ensure_running(self, trigger: str) -> dict[str, Any]:
        """Start a recompute unless one is already running or not applicable.

        Returns {"status": "started" | "running" | "skipped", ...}.
        """
        now = market_now()
        today = now.date()
        with self._lock:
            if self._running:
                return {"status": "running", "percent": self._percent, "message": self._message}
            if not is_trading_day(today):
                return {"status": "skipped", "reason": "non_trading_day"}
            # 午间休盘与收盘后（日K补库落库前）同样允许：报价分别是上午
            # 快照与当日收盘快照。
            if not is_past_market_open(now):
                return {"status": "skipped", "reason": "pre_open"}
            try:
                revision = get_db().get_market_dashboard_revision()
            except Exception:
                logger.exception("Failed to read dashboard revision")
                return {"status": "skipped", "reason": "revision_unavailable"}
            max_bar = str(revision[0])[:10] if revision and revision[0] else ""
            if max_bar >= today.isoformat():
                # 今日日K已落库，EOD 看板即为盘后确认值，无需实时估算。
                return {"status": "skipped", "reason": "eod_current"}
            self._running = True
            self._percent = 0.0
            self._message = "正在准备…"
            self._last_error = None

        thread = threading.Thread(target=self._run, args=(trigger,), daemon=True)
        thread.start()
        return {"status": "started"}

    # ------------------------------------------------------------------
    def _run(self, trigger: str) -> None:
        try:
            db = get_db()
            symbols = db.list_market_symbols(price_mode="qfq")
            if not symbols:
                raise RuntimeError("本地无日K数据")
            metadata_map = db.get_instrument_metadata_map()
            classified = filter_fully_classified(symbols, metadata_map)
            if not classified:
                raise RuntimeError("无完整分类的标的")

            data_service = DataService()
            try:
                payload = build_intraday_dashboard(
                    classified,
                    db=db,
                    data_service=data_service,
                    trend_config=trend_config(),
                    progress_callback=self._on_progress,
                )
            finally:
                data_service.close()

            computed_at = db.save_dashboard_snapshot("intraday", payload.get("as_of"), payload)
            snapshot = {
                "kind": "intraday",
                "as_of": payload.get("as_of"),
                "computed_at": computed_at,
                "payload": payload,
            }
            with self._lock:
                self._snapshot = snapshot
                self._snapshot_loaded = True
                self._last_finished_at = computed_at
                self._percent = 1.0
                self._message = "完成"
            logger.info(
                "Intraday dashboard snapshot saved (trigger=%s, as_of=%s)",
                trigger,
                snapshot["as_of"],
            )
        except Exception as exc:
            logger.exception("Intraday dashboard recompute failed (trigger=%s)", trigger)
            with self._lock:
                self._last_error = str(exc)
        finally:
            with self._lock:
                self._running = False

    def _on_progress(self, update: dict) -> None:
        with self._lock:
            self._percent = float(update.get("percent", 0))
            self._message = str(update.get("message", ""))


snapshot_runner = IntradaySnapshotRunner()
