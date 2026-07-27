from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
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


def _address_of(raw: str) -> str:
    """Extract the bare address from a "From" header value (e.g. 'Name <a@b.com>' -> 'a@b.com')."""
    return parseaddr(raw)[1].lower()


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
        conn.create_function("addr_of", 1, _address_of)
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

    @staticmethod
    def _conditions(
        *,
        rule_email: str | None,
        days: int | None,
    ) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        params: list[object] = []

        if rule_email:
            conditions.append("addr_of(sender) = ?")
            params.append(rule_email.lower())
        if days is not None:
            since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
            conditions.append("processed_at >= ?")
            params.append(since)

        return conditions, params

    def query(
        self,
        *,
        rule_email: str | None = None,
        days: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[LogEntry]:
        conditions, params = self._conditions(rule_email=rule_email, days=days)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params = params + [limit, offset]

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decisions {where} ORDER BY processed_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()

        return [LogEntry(**dict(row)) for row in rows]

    def count(
        self,
        *,
        rule_email: str | None = None,
        days: int | None = None,
    ) -> dict[str, int]:
        conditions, params = self._conditions(rule_email=rule_email, days=days)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT verdict, COUNT(*) as n FROM decisions {where} GROUP BY verdict",
                params,
            ).fetchall()
        return {row["verdict"]: row["n"] for row in rows}
