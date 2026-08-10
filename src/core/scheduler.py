from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from audit.app_logger import get_logger
from core.settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class SchedulerManager:
    settings: Settings
    scheduler: BackgroundScheduler | None = None

    def start(self, update_job: Callable[[], None]) -> None:
        if self.scheduler is not None:
            return

        scheduler = BackgroundScheduler(timezone=self.settings.app.timezone)

        upd_h, upd_m = self.settings.app.update_time_after_close.split(":")
        scheduler.add_job(
            update_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=int(upd_h), minute=int(upd_m)),
            id="daily_update",
            replace_existing=True,
            # 允许 2 小时内的 misfire 补跑（执行器繁忙/进程短暂卡顿）；
            # 进程完全离线造成的错过由 app.main 的启动补偿兜底。
            misfire_grace_time=7200,
            coalesce=True,
        )

        scheduler.start()
        self.scheduler = scheduler
        logger.info("Scheduler started with %s jobs", len(scheduler.get_jobs()))

    def shutdown(self) -> None:
        if self.scheduler is None:
            return
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped at %s", datetime.now().isoformat())
        self.scheduler = None

    def jobs_snapshot(self) -> list[dict[str, str]]:
        if self.scheduler is None:
            return []
        out: list[dict[str, str]] = []
        for job in self.scheduler.get_jobs():
            out.append({"id": job.id, "next_run": str(job.next_run_time)})
        return out
