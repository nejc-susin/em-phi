from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from em_phi.models import Verdict

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT UNIQUE NOT NULL,
    sender        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    verdict       TEXT NOT NULL CHECK(verdict IN ('relevant', 'irrelevant')),
    confidence    TEXT NOT NULL CHECK(confidence IN ('high', 'medium', 'low')),
    reason        TEXT NOT NULL,
    action_taken  TEXT NOT NULL CHECK(action_taken IN ('label', 'archive', 'keep')),
    processed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sender       ON decisions(sender);
CREATE INDEX IF NOT EXISTS idx_processed_at ON decisions(processed_at);
"""


@dataclass
class LogEntry:
    id: int
    message_id: str
    sender: str
    subject: str
    received_at: str
    verdict: str
    confidence: str
    reason: str
    action_taken: str
    processed_at: str


class DecisionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        logger.debug("DecisionLog: initialized at %s", self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def record(
        self,
        *,
        message_id: str,
        sender: str,
        subject: str,
        received_at: datetime,
        verdict: Verdict,
        action_taken: str,
    ) -> None:
        logger.debug("DecisionLog: recording %s (verdict=%s action=%s)", message_id, verdict.verdict, action_taken)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO decisions
                    (message_id, sender, subject, received_at,
                     verdict, confidence, reason, action_taken)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    sender,
                    subject,
                    received_at.isoformat(),
                    verdict.verdict,
                    verdict.confidence,
                    verdict.reason,
                    action_taken,
                ),
            )

    def query(
        self,
        *,
        sender: str | None = None,
        days: int | None = None,
        limit: int = 20,
    ) -> list[LogEntry]:
        conditions: list[str] = []
        params: list[object] = []

        if sender:
            conditions.append("sender = ?")
            params.append(sender)
        if days is not None:
            since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
            conditions.append("processed_at >= ?")
            params.append(since)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decisions {where} ORDER BY processed_at DESC LIMIT ?",
                params,
            ).fetchall()

        return [LogEntry(**dict(row)) for row in rows]

    def count(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT verdict, COUNT(*) as n FROM decisions GROUP BY verdict"
            ).fetchall()
        return {row["verdict"]: row["n"] for row in rows}
