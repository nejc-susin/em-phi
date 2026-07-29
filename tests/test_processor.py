from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from em_phi.actions import apply_verdict
from em_phi.config import LabelsConfig
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.processor import _process_rule


# ------------------------------------------------------------------
# actions.apply_verdict
# ------------------------------------------------------------------

def _make_provider() -> MagicMock:
    return MagicMock()


def test_relevant_email_gets_relevant_label(
    relevant_email, relevant_verdict, sample_rule, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=relevant_email,
        verdict=relevant_verdict,
        rule=sample_rule,
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once_with(relevant_email.message_id, sample_config.labels.relevant)
    provider.archive.assert_not_called()
    assert action == "label"


def test_irrelevant_label_only(
    irrelevant_email, irrelevant_verdict, sample_rule, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=irrelevant_email,
        verdict=irrelevant_verdict,
        rule=sample_rule,  # action="label"
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once_with(irrelevant_email.message_id, sample_config.labels.irrelevant)
    provider.archive.assert_not_called()
    assert action == "label"


def test_irrelevant_with_archive_action(
    irrelevant_email, irrelevant_verdict, sample_rule_archive, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=irrelevant_email,
        verdict=irrelevant_verdict,
        rule=sample_rule_archive,  # action="archive"
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once()
    provider.archive.assert_called_once_with(irrelevant_email.message_id)
    assert action == "archive"


def test_dry_run_makes_no_provider_calls(
    relevant_email, relevant_verdict, sample_rule, sample_config
) -> None:
    provider = _make_provider()
    apply_verdict(
        email=relevant_email,
        verdict=relevant_verdict,
        rule=sample_rule,
        labels=sample_config.labels,
        provider=provider,
        dry_run=True,
    )
    provider.apply_label.assert_not_called()
    provider.archive.assert_not_called()


# ------------------------------------------------------------------
# decision_log.DecisionLog
# ------------------------------------------------------------------

def test_decision_log_roundtrip(tmp_db: Path, relevant_email, relevant_verdict) -> None:
    log = DecisionLog(tmp_db)
    assert not log.is_processed(relevant_email.message_id)

    log.record(
        message_id=relevant_email.message_id,
        sender=relevant_email.sender,
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )

    assert log.is_processed(relevant_email.message_id)
    entries = log.query()
    assert len(entries) == 1
    assert entries[0].verdict == "relevant"
    assert entries[0].confidence == "high"


def test_decision_log_records_inbox_action(tmp_db: Path, relevant_email, relevant_verdict) -> None:
    """action_taken='inbox' (rule.action == 'inbox') must not be silently
    dropped — record() uses INSERT OR IGNORE, which swallows CHECK
    constraint violations without raising, so a stale allow-list here is a
    silent-data-loss bug, not a loud one."""
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender=relevant_email.sender,
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="inbox",
    )

    assert log.is_processed(relevant_email.message_id)
    entries = log.query()
    assert len(entries) == 1
    assert entries[0].action_taken == "inbox"


def test_decision_log_migrates_old_action_taken_check(tmp_db: Path) -> None:
    """Simulates opening a pre-existing decisions.db created before the
    action_taken CHECK constraint was dropped: data must survive, indexes
    must be recreated, and 'inbox' must work afterward."""
    import sqlite3

    old_schema = """
    CREATE TABLE decisions (
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
    CREATE INDEX idx_sender       ON decisions(sender);
    CREATE INDEX idx_processed_at ON decisions(processed_at);
    """
    conn = sqlite3.connect(tmp_db)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO decisions (message_id, sender, subject, received_at, verdict, confidence, reason, action_taken)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old-msg", "newsletter@example.com", "Pre-existing entry", "2026-01-01T00:00:00+00:00",
         "relevant", "high", "was fine", "label"),
    )
    conn.commit()
    conn.close()

    log = DecisionLog(tmp_db)  # should auto-migrate on open

    entries = log.query(limit=100)
    assert len(entries) == 1
    assert entries[0].message_id == "old-msg"

    # The migration must not have dropped the indexes it rebuilds.
    conn = sqlite3.connect(tmp_db)
    index_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    assert "idx_sender" in index_names
    assert "idx_processed_at" in index_names

    # And the actual bug is fixed: 'inbox' no longer gets silently dropped.
    log.record(
        message_id="new-msg",
        sender="newsletter@example.com",
        subject="New inbox-action entry",
        received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        verdict=Verdict(verdict="relevant", confidence="high", reason="matches interests"),
        action_taken="inbox",
    )
    assert log.is_processed("new-msg")
    assert len(log.query(limit=100)) == 2


def test_decision_log_duplicate_ignored(tmp_db: Path, relevant_email, relevant_verdict) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender=relevant_email.sender,
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )
    # Recording the same message_id again should not raise or duplicate
    log.record(
        message_id=relevant_email.message_id,
        sender=relevant_email.sender,
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )
    assert len(log.query(limit=100)) == 1


def test_decision_log_query_offset_pages_through_results(tmp_db: Path, relevant_email, relevant_verdict) -> None:
    log = DecisionLog(tmp_db)
    for i in range(3):
        log.record(
            message_id=f"msg{i}",
            sender=relevant_email.sender,
            subject=f"Subject {i}",
            received_at=relevant_email.received_at,
            verdict=relevant_verdict,
            action_taken="label",
        )

    page1 = log.query(limit=2, offset=0)
    page2 = log.query(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 1
    seen_ids = {e.message_id for e in page1} | {e.message_id for e in page2}
    assert seen_ids == {"msg0", "msg1", "msg2"}
    assert {e.message_id for e in page1}.isdisjoint({e.message_id for e in page2})


def test_decision_log_query_filter_by_rule(tmp_db: Path, relevant_email, irrelevant_email,
                                           relevant_verdict, irrelevant_verdict) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender="a@example.com",
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )
    log.record(
        message_id=irrelevant_email.message_id,
        sender="b@example.com",
        subject=irrelevant_email.subject,
        received_at=irrelevant_email.received_at,
        verdict=irrelevant_verdict,
        action_taken="label",
    )
    results = log.query(rule_email="a@example.com")
    assert len(results) == 1
    assert results[0].sender == "a@example.com"


def test_decision_log_count_respects_filters(tmp_db: Path, relevant_email, irrelevant_email,
                                              relevant_verdict, irrelevant_verdict) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender="a@example.com",
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )
    log.record(
        message_id=irrelevant_email.message_id,
        sender="b@example.com",
        subject=irrelevant_email.subject,
        received_at=irrelevant_email.received_at,
        verdict=irrelevant_verdict,
        action_taken="label",
    )

    assert log.count() == {"relevant": 1, "irrelevant": 1}
    assert log.count(rule_email="a@example.com") == {"relevant": 1}
    assert log.count(rule_email="b@example.com") == {"irrelevant": 1}


def test_decision_log_query_filter_by_verdict_action_and_search(
    tmp_db: Path, relevant_email, irrelevant_email, relevant_verdict, irrelevant_verdict,
) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender=relevant_email.sender,
        subject=relevant_email.subject,  # "Python 3.14 released"
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )
    log.record(
        message_id=irrelevant_email.message_id,
        sender=irrelevant_email.sender,
        subject=irrelevant_email.subject,  # "Join our community meetup"
        received_at=irrelevant_email.received_at,
        verdict=irrelevant_verdict,
        action_taken="archive",
    )

    assert [e.message_id for e in log.query(verdict="relevant")] == ["msg001"]
    assert [e.message_id for e in log.query(action="archive")] == ["msg002"]
    assert [e.message_id for e in log.query(search="python")] == ["msg001"]
    assert [e.message_id for e in log.query(search="meetup")] == ["msg002"]
    assert log.query(search="nonexistent") == []
    assert log.count(verdict="relevant") == {"relevant": 1}
    assert log.count(action="archive") == {"irrelevant": 1}


def test_decision_log_query_filter_matches_display_name_sender(
    tmp_db: Path, relevant_email, relevant_verdict,
) -> None:
    """Real 'From' headers include a display name; filtering by the bare
    configured address must still match."""
    log = DecisionLog(tmp_db)
    log.record(
        message_id=relevant_email.message_id,
        sender="Example Newsletter <newsletter@example.com>",
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )

    results = log.query(rule_email="newsletter@example.com")
    assert len(results) == 1
    results = log.query(rule_email="NEWSLETTER@EXAMPLE.COM")
    assert len(results) == 1


# ------------------------------------------------------------------
# processor._process_rule
# ------------------------------------------------------------------

def _make_rule_processor(
    *,
    message_ids: list[str],
    emails: dict,
    verdicts: dict,
    config,
    rule,
    tmp_db: Path,
    dry_run: bool = False,
) -> tuple:
    provider = MagicMock()
    provider.fetch_unread.return_value = message_ids
    provider.get_message.side_effect = lambda mid: emails[mid]

    classifier = MagicMock()
    classifier.classify.side_effect = lambda email, r: verdicts[email.message_id]

    log = DecisionLog(tmp_db)
    seen: list[tuple] = []

    result = _process_rule(
        rule=rule,
        config=config,
        provider=provider,
        classifier=classifier,
        log=log,
        dry_run=dry_run,
        on_email=lambda e, v, a, dr: seen.append((e.message_id, v.verdict, a)),
        on_error=None,
    )
    return result, log, provider, seen


def test_process_rule_basic(
    tmp_db, sample_config, sample_rule, relevant_email, irrelevant_email,
    relevant_verdict, irrelevant_verdict,
) -> None:
    result, log, provider, seen = _make_rule_processor(
        message_ids=["msg001", "msg002"],
        emails={"msg001": relevant_email, "msg002": irrelevant_email},
        verdicts={"msg001": relevant_verdict, "msg002": irrelevant_verdict},
        config=sample_config,
        rule=sample_rule,
        tmp_db=tmp_db,
    )
    assert result.processed == 2
    assert result.relevant == 1
    assert result.irrelevant == 1
    assert result.skipped == 0
    assert result.errors == 0
    assert log.is_processed("msg001")
    assert log.is_processed("msg002")


def test_process_rule_skips_already_processed(
    tmp_db, sample_config, sample_rule, relevant_email, relevant_verdict,
) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id="msg001",
        sender=sample_rule.email[0],
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )

    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    classifier = MagicMock()

    result = _process_rule(
        rule=sample_rule,
        config=sample_config,
        provider=provider,
        classifier=classifier,
        log=log,
        dry_run=False,
        on_email=None,
        on_error=None,
    )
    assert result.skipped == 1
    assert result.processed == 0
    classifier.classify.assert_not_called()


def test_process_rule_fetch_error_is_non_fatal(
    tmp_db, sample_config, sample_rule,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.side_effect = RuntimeError("Network error")
    classifier = MagicMock()
    errors: list[str] = []

    result = _process_rule(
        rule=sample_rule,
        config=sample_config,
        provider=provider,
        classifier=classifier,
        log=DecisionLog(tmp_db),
        dry_run=False,
        on_email=None,
        on_error=lambda ctx, exc: errors.append(str(exc)),
    )
    assert result.errors == 1
    assert result.processed == 0
    assert "Network error" in errors[0]


def test_process_rule_classify_error_is_non_fatal(
    tmp_db, sample_config, sample_rule, relevant_email,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    provider.get_message.return_value = relevant_email

    classifier = MagicMock()
    classifier.classify.side_effect = RuntimeError("Claude API error")

    errors: list[str] = []
    result = _process_rule(
        rule=sample_rule,
        config=sample_config,
        provider=provider,
        classifier=classifier,
        log=DecisionLog(tmp_db),
        dry_run=False,
        on_email=None,
        on_error=lambda ctx, exc: errors.append(str(exc)),
    )
    assert result.errors == 1
    assert result.processed == 0
    assert "Claude API error" in errors[0]


def test_process_rule_dry_run_does_not_log(
    tmp_db, sample_config, sample_rule, relevant_email, relevant_verdict,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    provider.get_message.return_value = relevant_email

    classifier = MagicMock()
    classifier.classify.return_value = relevant_verdict

    log = DecisionLog(tmp_db)
    _process_rule(
        rule=sample_rule,
        config=sample_config,
        provider=provider,
        classifier=classifier,
        log=log,
        dry_run=True,
        on_email=None,
        on_error=None,
    )

    assert not log.is_processed("msg001")
    provider.apply_label.assert_not_called()
