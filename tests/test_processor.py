from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from em_phi.actions import apply_verdict
from em_phi.config import LabelsConfig
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.processor import _process_sender


# ------------------------------------------------------------------
# actions.apply_verdict
# ------------------------------------------------------------------

def _make_provider() -> MagicMock:
    return MagicMock()


def test_relevant_email_gets_relevant_label(
    relevant_email, relevant_verdict, sample_sender, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=relevant_email,
        verdict=relevant_verdict,
        sender=sample_sender,
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once_with(relevant_email.message_id, sample_config.labels.relevant)
    provider.archive.assert_not_called()
    assert action == "label"


def test_irrelevant_label_only(
    irrelevant_email, irrelevant_verdict, sample_sender, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=irrelevant_email,
        verdict=irrelevant_verdict,
        sender=sample_sender,  # action="label"
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once_with(irrelevant_email.message_id, sample_config.labels.irrelevant)
    provider.archive.assert_not_called()
    assert action == "label"


def test_irrelevant_with_archive_action(
    irrelevant_email, irrelevant_verdict, sample_sender_archive, sample_config
) -> None:
    provider = _make_provider()
    action = apply_verdict(
        email=irrelevant_email,
        verdict=irrelevant_verdict,
        sender=sample_sender_archive,  # action="archive"
        labels=sample_config.labels,
        provider=provider,
        dry_run=False,
    )
    provider.apply_label.assert_called_once()
    provider.archive.assert_called_once_with(irrelevant_email.message_id)
    assert action == "archive"


def test_dry_run_makes_no_provider_calls(
    relevant_email, relevant_verdict, sample_sender, sample_config
) -> None:
    provider = _make_provider()
    apply_verdict(
        email=relevant_email,
        verdict=relevant_verdict,
        sender=sample_sender,
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


def test_decision_log_query_filter_by_sender(tmp_db: Path, relevant_email, irrelevant_email,
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
    results = log.query(sender="a@example.com")
    assert len(results) == 1
    assert results[0].sender == "a@example.com"


# ------------------------------------------------------------------
# processor._process_sender
# ------------------------------------------------------------------

def _make_sender_processor(
    *,
    message_ids: list[str],
    emails: dict,
    verdicts: dict,
    config,
    sender,
    tmp_db: Path,
    dry_run: bool = False,
) -> tuple:
    provider = MagicMock()
    provider.fetch_unread.return_value = message_ids
    provider.get_message.side_effect = lambda mid: emails[mid]

    classifier = MagicMock()
    classifier.classify.side_effect = lambda email, s: verdicts[email.message_id]

    log = DecisionLog(tmp_db)
    seen: list[tuple] = []

    result = _process_sender(
        sender=sender,
        config=config,
        provider=provider,
        classifier=classifier,
        log=log,
        dry_run=dry_run,
        on_email=lambda e, v, a, dr: seen.append((e.message_id, v.verdict, a)),
        on_error=None,
    )
    return result, log, provider, seen


def test_process_sender_basic(
    tmp_db, sample_config, sample_sender, relevant_email, irrelevant_email,
    relevant_verdict, irrelevant_verdict,
) -> None:
    result, log, provider, seen = _make_sender_processor(
        message_ids=["msg001", "msg002"],
        emails={"msg001": relevant_email, "msg002": irrelevant_email},
        verdicts={"msg001": relevant_verdict, "msg002": irrelevant_verdict},
        config=sample_config,
        sender=sample_sender,
        tmp_db=tmp_db,
    )
    assert result.processed == 2
    assert result.relevant == 1
    assert result.irrelevant == 1
    assert result.skipped == 0
    assert result.errors == 0
    assert log.is_processed("msg001")
    assert log.is_processed("msg002")


def test_process_sender_skips_already_processed(
    tmp_db, sample_config, sample_sender, relevant_email, relevant_verdict,
) -> None:
    log = DecisionLog(tmp_db)
    log.record(
        message_id="msg001",
        sender=sample_sender.email,
        subject=relevant_email.subject,
        received_at=relevant_email.received_at,
        verdict=relevant_verdict,
        action_taken="label",
    )

    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    classifier = MagicMock()

    result = _process_sender(
        sender=sample_sender,
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


def test_process_sender_fetch_error_is_non_fatal(
    tmp_db, sample_config, sample_sender,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.side_effect = RuntimeError("Network error")
    classifier = MagicMock()
    errors: list[str] = []

    result = _process_sender(
        sender=sample_sender,
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


def test_process_sender_classify_error_is_non_fatal(
    tmp_db, sample_config, sample_sender, relevant_email,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    provider.get_message.return_value = relevant_email

    classifier = MagicMock()
    classifier.classify.side_effect = RuntimeError("Claude API error")

    errors: list[str] = []
    result = _process_sender(
        sender=sample_sender,
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


def test_process_sender_dry_run_does_not_log(
    tmp_db, sample_config, sample_sender, relevant_email, relevant_verdict,
) -> None:
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    provider.get_message.return_value = relevant_email

    classifier = MagicMock()
    classifier.classify.return_value = relevant_verdict

    log = DecisionLog(tmp_db)
    _process_sender(
        sender=sample_sender,
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
