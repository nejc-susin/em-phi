import json
import sqlite3
from pathlib import Path

from em_phi.decision_log import DecisionLog
from em_phi.processor import RuleResult, RunSummary
from em_phi.run_log import (
    INTERRUPTED_FAILURE,
    MAX_ERROR_LENGTH,
    MAX_ERROR_MESSAGES,
    ErrorCollector,
    RunLog,
    format_duration,
)


def _summary(**counts) -> RunSummary:
    return RunSummary(results=[RuleResult(rule_email="newsletter@example.com", **counts)])


def _set_started_at(db: Path, run_id: int, started_at: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (started_at, run_id))


def test_shares_the_decision_log_database(tmp_db: Path) -> None:
    DecisionLog(tmp_db)
    RunLog(tmp_db)

    with sqlite3.connect(tmp_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"decisions", "runs"} <= tables


def test_start_creates_a_running_row(tmp_db: Path) -> None:
    log = RunLog(tmp_db)

    run_id = log.start(trigger="manual", dry_run=True, rule_filter="newsletter@example.com")

    run = log.last()
    assert run is not None and run.id == run_id
    assert (run.trigger, run.dry_run, run.rule_filter, run.status) == (
        "manual", True, "newsletter@example.com", "running",
    )
    assert run.finished_at is None
    assert run.duration_seconds is None
    assert run.error_messages == []
    assert len(run.started_at) == len("2026-05-01 08:00:00")


def test_finish_without_errors_is_ok(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    run_id = log.start(trigger="cli", dry_run=False, rule_filter=None)

    log.finish(run_id, _summary(processed=3, relevant=2, irrelevant=1, skipped=4), [])

    run = log.last()
    assert run is not None
    assert (run.processed, run.relevant, run.irrelevant, run.skipped, run.errors) == (3, 2, 1, 4, 0)
    assert run.status == "ok"
    assert run.finished_at is not None
    assert run.duration_seconds is not None and run.duration_seconds >= 0


def test_finish_with_errors_has_errors_status(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    run_id = log.start(trigger="scheduled", dry_run=False, rule_filter=None)

    log.finish(run_id, _summary(processed=1, relevant=1, errors=1), ["processing message m1: boom"])

    run = log.last()
    assert run is not None
    assert (run.status, run.errors) == ("errors", 1)
    assert run.error_messages == ["processing message m1: boom"]


def test_fail_records_the_failure(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    run_id = log.start(trigger="scheduled", dry_run=False, rule_filter=None)

    log.fail(run_id, "ANTHROPIC_API_KEY environment variable is not set.", ["earlier: oops"])

    run = log.last()
    assert run is not None
    assert run.status == "failed"
    assert run.failure == "ANTHROPIC_API_KEY environment variable is not set."
    assert run.error_messages == ["earlier: oops"]
    assert run.finished_at is not None


def test_error_collector_caps_and_truncates_and_forwards() -> None:
    forwarded: list[tuple[str, str]] = []
    collector = ErrorCollector(lambda context, exc: forwarded.append((context, str(exc))))

    collector("processing message m0", RuntimeError("x" * 2000))
    for i in range(1, MAX_ERROR_MESSAGES + 10):
        collector(f"processing message m{i}", RuntimeError("boom"))

    assert len(collector.messages) == MAX_ERROR_MESSAGES
    assert len(collector.messages[0]) == MAX_ERROR_LENGTH
    assert collector.messages[0].startswith("processing message m0: xxx")
    assert collector.messages[1] == "processing message m1: boom"
    # Every error is still forwarded, even past the cap.
    assert len(forwarded) == MAX_ERROR_MESSAGES + 10


def test_error_collector_without_forward() -> None:
    collector = ErrorCollector()
    collector("fetch_unread(['a@x.com'])", RuntimeError("Gmail API error"))
    assert collector.messages == ["fetch_unread(['a@x.com']): Gmail API error"]


def test_stored_error_messages_are_capped(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    run_id = log.start(trigger="cli", dry_run=False, rule_filter=None)

    log.finish(run_id, _summary(errors=60), ["e" * 900] * 60)

    run = log.last()
    assert run is not None
    assert len(run.error_messages) == MAX_ERROR_MESSAGES
    assert all(len(m) == MAX_ERROR_LENGTH for m in run.error_messages)
    with sqlite3.connect(tmp_db) as conn:
        raw = conn.execute("SELECT error_messages FROM runs").fetchone()[0]
    assert isinstance(json.loads(raw), list)


def test_recent_is_newest_first_and_limited(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    ids = [log.start(trigger="cli", dry_run=False, rule_filter=None) for _ in range(5)]

    assert [r.id for r in log.recent()] == list(reversed(ids))
    assert [r.id for r in log.recent(limit=2)] == [ids[4], ids[3]]


def test_last_is_none_when_empty(tmp_db: Path) -> None:
    assert RunLog(tmp_db).last() is None
    assert RunLog(tmp_db).recent() == []


def test_last_successful_gmail_access_skips_failed_and_running_runs(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    assert log.last_successful_gmail_access() is None

    with_errors = log.start(trigger="scheduled", dry_run=False, rule_filter=None)
    log.finish(with_errors, _summary(errors=1), ["x: y"])
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE runs SET finished_at = '2026-10-01 06:00:00' WHERE id = ?", (with_errors,))
    failed = log.start(trigger="scheduled", dry_run=False, rule_filter=None)
    log.fail(failed, "invalid_grant", [])
    log.start(trigger="manual", dry_run=False, rule_filter=None)  # still running

    assert log.last_successful_gmail_access() == "2026-10-01 06:00:00"


def test_mark_interrupted_only_touches_old_running_rows(tmp_db: Path) -> None:
    log = RunLog(tmp_db)
    old_running = log.start(trigger="manual", dry_run=False, rule_filter=None)
    _set_started_at(tmp_db, old_running, "2020-01-01 00:00:00")
    old_finished = log.start(trigger="manual", dry_run=False, rule_filter=None)
    log.finish(old_finished, _summary(processed=1, relevant=1), [])
    _set_started_at(tmp_db, old_finished, "2020-01-01 00:00:00")
    fresh_running = log.start(trigger="cli", dry_run=False, rule_filter=None)

    assert log.mark_interrupted() == 1

    by_id = {r.id: r for r in log.recent()}
    assert by_id[old_running].status == "failed"
    assert by_id[old_running].failure == INTERRUPTED_FAILURE
    assert by_id[old_finished].status == "ok"
    assert by_id[fresh_running].status == "running"
    assert log.mark_interrupted() == 0


def test_format_duration() -> None:
    assert format_duration(None) == "—"
    assert format_duration(4.4) == "4s"
    assert format_duration(185) == "3m 05s"
    assert format_duration(3720) == "1h 02m"
