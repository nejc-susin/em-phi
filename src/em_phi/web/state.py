from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from em_phi.config import AppConfig
from em_phi.processor import RunSummary


@dataclass
class LastRun:
    summary: RunSummary
    dry_run: bool
    finished_at: datetime


class AppState:
    """Shared mutable state for the web server."""

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.is_running: bool = False
        self.run_lock: asyncio.Lock = asyncio.Lock()
        self.last_run: LastRun | None = None

    def reload_config(self, new_config: AppConfig) -> None:
        self.config = new_config
