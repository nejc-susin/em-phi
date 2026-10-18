from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from em_phi.config import AppConfig

logger = logging.getLogger(__name__)

_JOB_ID = "em_phi_run"


class EmPhiScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._description: str | None = None

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
            self._description = None
            logger.info("Scheduler: disabled (schedule.enabled=false)")
            return
        self._scheduler, desc = built
        self._description = desc
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
        scheduler.add_job(run_fn, trigger, id=_JOB_ID, replace_existing=True)
        return scheduler, desc

    def description(self) -> str | None:
        """Human description of the active schedule ("every 6h", "cron '…'"), None when disabled."""
        return self._description

    def next_run_time(self) -> datetime | None:
        """Next fire time of the run job (timezone-aware), None when disabled or not running."""
        if self._description is None or not self._scheduler.running:
            return None
        job = self._scheduler.get_job(_JOB_ID)
        return job.next_run_time if job is not None else None

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler: stopped")
