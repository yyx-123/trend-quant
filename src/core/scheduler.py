from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from audit.app_logger import get_logger
from core.settings import Settings

logger = get_logger(__name__)

# 标的大盘盘中快照的 cron 触发计划（本地时间）：交易时段内每 5 分钟一轮
# —— 9:35~11:30、13:00~15:00，含 15:00 收盘快照；午间休盘报价不更新，不安排。
# 单轮重算约 20 秒，远小于 5 分钟间隔。元素为 (hour, minute) cron 表达式。
INTRADAY_SNAPSHOT_CRONS: tuple[tuple[str, str], ...] = (
    ("9", "35-59/5"),
    ("10", "*/5"),
    ("11", "0-30/5"),
    ("13-14", "*/5"),
    ("15", "0"),
)


@dataclass(slots=True)
class SchedulerManager:
    settings: Settings
    scheduler: BackgroundScheduler | None = None

    def start(
        self,
        update_job: Callable[[], None],
        intraday_snapshot_job: Callable[[], None] | None = None,
        industry_sync_job: Callable[[], None] | None = None,
    ) -> None:
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

        if industry_sync_job is not None:
            # 申万行业分类月度同步（TickFlow universes，免费）：每月 1 日凌晨
            # 低峰执行。申万官方每年 6/12 月调整成分、新股陆续纳入，月度足够；
            # 错过一两天无伤大雅，misfire 宽限 24h。
            scheduler.add_job(
                industry_sync_job,
                trigger=CronTrigger(day=1, hour=4, minute=30),
                id="stock_industry_sync",
                replace_existing=True,
                misfire_grace_time=86400,
                coalesce=True,
            )

        if intraday_snapshot_job is not None:
            # 标的大盘盘中快照：周一~周五交易时段每 5 分钟触发。任务内部
            # 再以 is_trading_day 兜底跳过节假日；单例运行器保证上一轮
            # 未结束时重复触发直接复用，不并发。间隔短，misfire 宽限 60s
            # 即可 —— 错过的一轮很快由下一轮接替。
            for idx, (hour, minute) in enumerate(INTRADAY_SNAPSHOT_CRONS):
                scheduler.add_job(
                    intraday_snapshot_job,
                    trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
                    id=f"intraday_snapshot_{idx}",
                    replace_existing=True,
                    misfire_grace_time=60,
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
