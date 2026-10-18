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
        self._activate(self._build(config, run_fn))

    def reschedule(self, config: AppConfig, run_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        # Configure the replacement before touching the running scheduler, so
        # a bad schedule raises here and leaves the old one running.
        built = self._build(config, run_fn)
        self._scheduler.remove_all_jobs()
        self.shutdown()
        self._scheduler = AsyncIOScheduler()
        self._activate(built)

    def _activate(self, built: tuple[AsyncIOScheduler, str] | None) -> None:
        if built is None:
            logger.info("Scheduler: disabled (schedule.enabled=false)")
            return
        self._scheduler, desc = built
        self._scheduler.start()
        logger.info("Scheduler: started (%s)", desc)

    @staticmethod
    def _build(
        config: AppConfig, run_fn: Callable[[], Coroutine[Any, Any, None]]
    ) -> tuple[AsyncIOScheduler, str] | None:
        """Return a configured, not yet started scheduler, or None if disabled."""
        sc = config.schedule
        if not sc.enabled:
            return None

        if sc.cron:
            trigger = CronTrigger.from_crontab(sc.cron)
            desc = f"cron '{sc.cron}'"
        else:
            hours = sc.interval_hours or 6
            trigger = IntervalTrigger(hours=hours)
            desc = f"every {hours}h"

        scheduler = AsyncIOScheduler()
        scheduler.add_job(run_fn, trigger, id="em_phi_run", replace_existing=True)
        return scheduler, desc

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler: stopped")
