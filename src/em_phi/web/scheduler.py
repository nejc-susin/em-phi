from __future__ import annotations

import logging
from typing import Callable, Coroutine, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from em_phi.config import AppConfig

logger = logging.getLogger(__name__)


class EmPhiScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def start(self, config: AppConfig, run_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        sc = config.schedule
        if not sc.enabled:
            logger.info("Scheduler: disabled (schedule.enabled=false)")
            return

        if sc.cron:
            trigger = CronTrigger.from_crontab(sc.cron)
            desc = f"cron '{sc.cron}'"
        else:
            hours = sc.interval_hours or 6
            trigger = IntervalTrigger(hours=hours)
            desc = f"every {hours}h"

        self._scheduler.add_job(run_fn, trigger, id="em_phi_run", replace_existing=True)
        self._scheduler.start()
        logger.info("Scheduler: started (%s)", desc)

    def reschedule(self, config: AppConfig, run_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._scheduler.remove_all_jobs()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._scheduler = AsyncIOScheduler()
        self.start(config, run_fn)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler: stopped")
