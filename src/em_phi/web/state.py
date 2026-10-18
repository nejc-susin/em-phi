from __future__ import annotations

import asyncio
from pathlib import Path

from em_phi.config import AppConfig
from em_phi.processor import RunSummary


class AppState:
    """Shared mutable state for the web server."""

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        # Held from the moment a run is started until its worker thread has
        # finished — see web.routes.run._start_run.
        self.run_lock: asyncio.Lock = asyncio.Lock()
        # Strong reference to the in-flight run task so it isn't garbage-collected.
        self.current_run: asyncio.Task[RunSummary] | None = None

    @property
    def is_running(self) -> bool:
        return self.run_lock.locked()

    def reload_config(self, new_config: AppConfig) -> None:
        self.config = new_config
