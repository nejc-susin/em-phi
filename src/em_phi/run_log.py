from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from em_phi.processor import OnError, RunSummary

logger = logging.getLogger(__name__)

TRIGGERS = ("manual", "scheduled", "cli")
STATUSES = ("running", "ok", "errors", "failed")
MAX_ERROR_MESSAGES = 50
MAX_ERROR_LENGTH = 500
INTERRUPTED_FAILURE = "Interrupted (process exited before the run finished)"

# Same format as SQLite's datetime('now'), which the decisions table uses.
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# "trigger" is an SQL keyword, so it is always quoted.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    "trigger"       TEXT NOT NULL,
    dry_run         INTEGER NOT NULL,
    rule_filter     TEXT,
    processed       INTEGER NOT NULL DEFAULT 0,
    relevant        INTEGER NOT NULL DEFAULT 0,
    irrelevant      INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    failure         TEXT,
    error_messages  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime(_TIME_FORMAT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def _truncate(message: str) -> str:
    if len(message) <= MAX_ERROR_LENGTH:
        return message
    return message[: MAX_ERROR_LENGTH - 1] + "…"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass
class RunRecord:
    id: int
    started_at: str  # UTC, "%Y-%m-%d %H:%M:%S"
    finished_at: str | None
    trigger: str
    dry_run: bool
    rule_filter: str | None
    processed: int
    relevant: int
    irrelevant: int
    skipped: int
    errors: int
    status: str
    failure: str | None
    error_messages: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (_parse(self.finished_at) - _parse(self.started_at)).total_seconds()

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration_seconds)

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> RunRecord:
        data = dict(row)
        data["dry_run"] = bool(data["dry_run"])
        try:
            messages = json.loads(data["error_messages"] or "[]")
        except ValueError:
            messages = []
        data["error_messages"] = [str(m) for m in messages] if isinstance(messages, list) else []
        return cls(**data)


class ErrorCollector:
    """An on_error callback that keeps "<context>: <message>" strings for the run log.

    Keeps at most MAX_ERROR_MESSAGES, each truncated to MAX_ERROR_LENGTH, and
    forwards every call to `forward` when given. Called from the worker thread.
    """

    def __init__(self, forward: OnError | None = None) -> None:
        self.messages: list[str] = []
        self._forward = forward

    def __call__(self, context: str, exc: Exception) -> None:
        if len(self.messages) < MAX_ERROR_MESSAGES:
            self.messages.append(_truncate(f"{context}: {exc}"))
        if self._forward is not None:
            self._forward(context, exc)


class RunLog:
    """Run history, kept in the `runs` table of the decision log's SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

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

    def start(self, *, trigger: str, dry_run: bool, rule_filter: str | None) -> int:
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown run trigger {trigger!r}")
        with self._connect() as conn:
            cur = conn.execute(
                'INSERT INTO runs (started_at, "trigger", dry_run, rule_filter, status) '
                "VALUES (?, ?, ?, ?, 'running')",
                (_now(), trigger, int(dry_run), rule_filter),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def finish(self, run_id: int, summary: RunSummary, error_messages: list[str]) -> None:
        status = "errors" if (summary.errors or error_messages) else "ok"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at = ?, processed = ?, relevant = ?, irrelevant = ?,
                    skipped = ?, errors = ?, status = ?, error_messages = ?
                WHERE id = ?
                """,
                (
                    _now(), summary.processed, summary.relevant, summary.irrelevant,
                    summary.skipped, summary.errors, status, _encode(error_messages), run_id,
                ),
            )

    def fail(self, run_id: int, failure: str, error_messages: list[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = 'failed', failure = ?, error_messages = ? "
                "WHERE id = ?",
                (_now(), failure, _encode(error_messages), run_id),
            )

    def recent(self, limit: int = 10) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [RunRecord._from_row(row) for row in rows]

    def last(self) -> RunRecord | None:
        runs = self.recent(1)
        return runs[0] if runs else None

    def last_successful_gmail_access(self) -> str | None:
        """finished_at of the newest run that got past authentication."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT finished_at FROM runs WHERE status IN ('ok', 'errors') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["finished_at"] if row else None

    def mark_interrupted(self, older_than_hours: int = 6) -> int:
        """Fail runs left 'running' by a process that exited mid-run.

        Only rows older than `older_than_hours` are touched, so a run still in
        progress in another process (e.g. a CLI run) is left alone.
        """
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=older_than_hours)).strftime(_TIME_FORMAT)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET status = 'failed', failure = ? WHERE status = 'running' AND started_at < ?",
                (INTERRUPTED_FAILURE, cutoff),
            )
            count = cur.rowcount
        if count:
            logger.info("RunLog: marked %d interrupted run(s) as failed", count)
        return count


def _encode(messages: list[str]) -> str:
    return json.dumps([_truncate(m) for m in messages[:MAX_ERROR_MESSAGES]])


class RunRecorder:
    """Records a single run. Run-log write failures are logged, never raised.

    `errors` is the on_error callback to hand to process_all; it collects the
    messages for the run log and forwards to `on_error` when given.
    """

    def __init__(
        self,
        path: Path,
        *,
        trigger: str,
        dry_run: bool,
        rule_filter: str | None,
        on_error: OnError | None = None,
    ) -> None:
        self.errors = ErrorCollector(on_error)
        self._log: RunLog | None = None
        self._run_id: int | None = None
        try:
            log = RunLog(path)
            self._run_id = log.start(trigger=trigger, dry_run=dry_run, rule_filter=rule_filter)
            self._log = log
        except Exception as exc:
            logger.warning("Could not record the run start in the run log: %s", exc)

    def finish(self, summary: RunSummary) -> None:
        if self._log is None or self._run_id is None:
            return
        try:
            self._log.finish(self._run_id, summary, self.errors.messages)
        except Exception as exc:
            logger.warning("Could not record the run result in the run log: %s", exc)

    def fail(self, exc: BaseException) -> None:
        if self._log is None or self._run_id is None:
            return
        try:
            self._log.fail(self._run_id, str(exc) or type(exc).__name__, self.errors.messages)
        except Exception as log_exc:
            logger.warning("Could not record the run failure in the run log: %s", log_exc)
