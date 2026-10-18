from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Iterator

from em_phi.config import RuleConfig
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
    action_taken  TEXT NOT NULL,
    rule_name     TEXT,
    processed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sender       ON decisions(sender);
CREATE INDEX IF NOT EXISTS idx_processed_at ON decisions(processed_at);
"""

# action_taken has no CHECK constraint: it comes from our own apply_verdict()
# (not external input), and record() uses INSERT OR IGNORE — a CHECK mismatch
# there doesn't raise, it silently drops the row. That bit us once already
# ("inbox" wasn't in the old allowed list) and would happen again for any
# future rule.action value we forget to add to a hardcoded list. Trust the
# internal caller instead of re-validating an invariant only our own code
# controls.
_OLD_ACTION_TAKEN_CHECK_MARKER = "CHECK(action_taken"

# idx_rule_name lives outside _SCHEMA: on a database created before rule_name
# existed, the column has to be added before an index on it can be created.
_RULE_NAME_INDEX = "CREATE INDEX IF NOT EXISTS idx_rule_name ON decisions(rule_name);"


def _address_of(raw: str) -> str:
    """Extract the bare address from a "From" header value (e.g. 'Name <a@b.com>' -> 'a@b.com')."""
    return parseaddr(raw)[1].lower()


def _rule_matches(address: str, patterns: list[str]) -> bool:
    """Mirror the Gmail provider's sender matching: full address, or bare domain."""
    for pattern in patterns:
        pattern = pattern.lower()
        if "@" in pattern:
            if address == pattern:
                return True
        elif address.endswith("@" + pattern):
            return True
    return False


def _like_escape(term: str) -> str:
    """Escape SQLite LIKE wildcards so a literal % or _ in search text isn't treated as a wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    rule_name: str | None
    processed_at: str


class DecisionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            if self._needs_action_taken_migration(conn):
                logger.info("DecisionLog: migrating 'decisions' table to drop the action_taken CHECK constraint")
                # One script, one transaction: executescript() COMMITs any pending
                # transaction first, so separate calls would each commit on their own
                # and a mid-way failure would strand the data in the backup table.
                # On failure the transaction stays open and _connect() rolls it back.
                conn.executescript(
                    "BEGIN;\n"
                    "ALTER TABLE decisions RENAME TO decisions_pre_action_taken_migration;\n"
                    "DROP INDEX IF EXISTS idx_sender;\n"
                    "DROP INDEX IF EXISTS idx_processed_at;\n"
                    + _SCHEMA  # recreates 'decisions' (new schema) + its indexes
                    + """
                    INSERT INTO decisions
                        (id, message_id, sender, subject, received_at,
                         verdict, confidence, reason, action_taken, processed_at)
                    SELECT id, message_id, sender, subject, received_at,
                           verdict, confidence, reason, action_taken, processed_at
                    FROM decisions_pre_action_taken_migration;
                    DROP TABLE decisions_pre_action_taken_migration;
                    COMMIT;
                    """
                )
            else:
                conn.executescript(_SCHEMA)
            self._ensure_rule_name_column(conn)
        logger.debug("DecisionLog: initialized at %s", self.path)

    @staticmethod
    def _needs_action_taken_migration(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone()
        return bool(row and row[0] and _OLD_ACTION_TAKEN_CHECK_MARKER in row[0])

    @staticmethod
    def _ensure_rule_name_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
        if "rule_name" not in columns:
            logger.info("DecisionLog: adding 'rule_name' column to 'decisions'")
            try:
                conn.execute("ALTER TABLE decisions ADD COLUMN rule_name TEXT")
            except sqlite3.OperationalError as exc:
                # Another process (CLI run vs. web server) added it between our check and here.
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute(_RULE_NAME_INDEX)

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
        rule_name: str,
    ) -> None:
        logger.debug("DecisionLog: recording %s (verdict=%s action=%s)", message_id, verdict.verdict, action_taken)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO decisions
                    (message_id, sender, subject, received_at,
                     verdict, confidence, reason, action_taken, rule_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    rule_name,
                ),
            )

    def backfill_rule_names(self, rules: list[RuleConfig]) -> int:
        """Fill in rule_name for rows logged before it was recorded.

        Each NULL row gets the first rule (in config order) whose patterns match
        its sender; rows matching no rule stay NULL. Returns the rows updated.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT id, sender FROM decisions WHERE rule_name IS NULL").fetchall()
            updates: list[tuple[str, int]] = []
            for row in rows:
                address = _address_of(row["sender"])
                for rule in rules:
                    if _rule_matches(address, rule.email):
                        updates.append((rule.name, row["id"]))
                        break
            conn.executemany("UPDATE decisions SET rule_name = ? WHERE id = ?", updates)
        if updates:
            logger.info("DecisionLog: backfilled rule_name on %d rows", len(updates))
        return len(updates)

    @staticmethod
    def _conditions(
        *,
        rule_email: str | None,
        days: int | None,
        verdict: str | None = None,
        action: str | None = None,
        search: str | None = None,
        rule_name: str | None = None,
    ) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        params: list[object] = []

        if rule_email:
            if "@" in rule_email:
                conditions.append("addr_of(sender) = ?")
                params.append(rule_email.lower())
            else:
                # Bare domain rule: matches every sender at that domain.
                conditions.append("addr_of(sender) LIKE ? ESCAPE '\\'")
                params.append("%@" + _like_escape(rule_email.lower()))
        if rule_name:
            conditions.append("rule_name = ?")
            params.append(rule_name)
        if days is not None:
            # Same format as SQLite's datetime('now') so the string compare is correct.
            since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            conditions.append("processed_at >= ?")
            params.append(since)
        if verdict:
            conditions.append("verdict = ?")
            params.append(verdict)
        if action:
            conditions.append("action_taken = ?")
            params.append(action)
        if search:
            conditions.append("(subject LIKE ? ESCAPE '\\' OR reason LIKE ? ESCAPE '\\')")
            pattern = f"%{_like_escape(search)}%"
            params.append(pattern)
            params.append(pattern)

        return conditions, params

    def query(
        self,
        *,
        rule_email: str | None = None,
        days: int | None = None,
        verdict: str | None = None,
        action: str | None = None,
        search: str | None = None,
        rule_name: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[LogEntry]:
        conditions, params = self._conditions(
            rule_email=rule_email, days=days, verdict=verdict, action=action, search=search,
            rule_name=rule_name,
        )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params = params + [limit, offset]

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decisions {where} ORDER BY processed_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()

        return [LogEntry(**dict(row)) for row in rows]

    def count(
        self,
        *,
        rule_email: str | None = None,
        days: int | None = None,
        verdict: str | None = None,
        action: str | None = None,
        search: str | None = None,
        rule_name: str | None = None,
    ) -> dict[str, int]:
        conditions, params = self._conditions(
            rule_email=rule_email, days=days, verdict=verdict, action=action, search=search,
            rule_name=rule_name,
        )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT verdict, COUNT(*) as n FROM decisions {where} GROUP BY verdict",
                params,
            ).fetchall()
        return {row["verdict"]: row["n"] for row in rows}
