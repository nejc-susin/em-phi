import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from em_phi.config import RuleConfig
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.playground import PlaygroundItem, run_playground, trial_rule


def _email(message_id: str, subject: str = "s", body: str = "body https://example.com/x") -> Email:
    return Email(
        message_id=message_id,
        sender="newsletter@example.com",
        subject=subject,
        body=body,
        received_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
    )


def _provider(message_ids: list[str]) -> MagicMock:
    provider = MagicMock()
    provider.fetch_recent.return_value = message_ids
    provider.get_message.side_effect = lambda mid: _email(mid, subject=f"subject {mid}")
    return provider


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    finally:
        conn.close()


def _record(log: DecisionLog, message_id: str, verdict: str) -> None:
    log.record(
        message_id=message_id,
        sender="newsletter@example.com",
        subject="s",
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        verdict=Verdict(verdict=verdict, confidence="high", reason="logged"),  # type: ignore[arg-type]
        action_taken="label",
        rule_name="Example Newsletter",
    )


def test_fetch_recent_called_with_rule_patterns_days_and_limit(sample_rule: RuleConfig) -> None:
    provider = _provider([])
    result = run_playground(provider=provider, classifier=None, log=None, rule=sample_rule, days=7, limit=5)
    provider.fetch_recent.assert_called_once_with(sample_rule.email, 7, 5)
    assert result.items == [] and result.fetch_errors == []


def test_order_preserved_with_concurrent_classify(sample_rule: RuleConfig) -> None:
    ids = [f"m{i}" for i in range(8)]
    provider = _provider(ids)
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def classify(email: Email, rule: RuleConfig) -> Verdict:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Earlier messages finish last, so completion order != fetch order.
        time.sleep(0.005 * (len(ids) - int(email.message_id[1:])))
        with lock:
            in_flight -= 1
        return Verdict(verdict="relevant", confidence="high", reason=email.message_id)

    classifier = MagicMock()
    classifier.classify.side_effect = classify

    result = run_playground(
        provider=provider, classifier=classifier, log=None, rule=sample_rule, days=14, limit=10, max_workers=4,
    )

    assert [i.email.message_id for i in result.items] == ids
    assert [i.verdict.reason for i in result.items] == ids  # each verdict on its own item
    assert peak > 1


def test_classifier_sees_preprocessed_email_and_trial_rule(sample_rule: RuleConfig) -> None:
    provider = _provider(["m1"])
    classifier = MagicMock()
    classifier.classify.return_value = Verdict(verdict="relevant", confidence="high", reason="r")
    trial = trial_rule(sample_rule, "Only Rust.", "aggressive")

    result = run_playground(provider=provider, classifier=classifier, log=None, rule=trial, days=14, limit=10)

    item = result.items[0]
    assert item.email.body == "body https://example.com/x"
    assert item.processed_email.body == "body <link>"
    email_arg, rule_arg = classifier.classify.call_args.args
    assert email_arg == item.processed_email
    assert rule_arg is trial
    assert "Only Rust." in item.system_prompt and "aggressive" in item.system_prompt
    assert "body <link>" in item.user_message


def test_per_item_classify_error_is_recorded_without_aborting(sample_rule: RuleConfig) -> None:
    provider = _provider(["m1", "m2", "m3"])

    def classify(email: Email, rule: RuleConfig) -> Verdict:
        if email.message_id == "m2":
            raise ValueError("Could not parse verdict")
        return Verdict(verdict="irrelevant", confidence="low", reason="r")

    classifier = MagicMock()
    classifier.classify.side_effect = classify

    result = run_playground(provider=provider, classifier=classifier, log=None, rule=sample_rule, days=14, limit=10)

    assert [i.email.message_id for i in result.items] == ["m1", "m2", "m3"]
    assert result.items[1].error == "Could not parse verdict"
    assert result.items[1].verdict is None
    assert result.items[0].verdict is not None and result.items[2].verdict is not None
    assert result.items[0].error is None


def test_get_message_failure_goes_to_fetch_errors(sample_rule: RuleConfig) -> None:
    provider = _provider(["m1", "bad", "m3"])

    def get_message(mid: str) -> Email:
        if mid == "bad":
            raise RuntimeError("Gmail API error")
        return _email(mid)

    provider.get_message.side_effect = get_message
    classifier = MagicMock()
    classifier.classify.return_value = Verdict(verdict="relevant", confidence="high", reason="r")

    result = run_playground(provider=provider, classifier=classifier, log=None, rule=sample_rule, days=14, limit=10)

    assert [i.email.message_id for i in result.items] == ["m1", "m3"]
    assert result.fetch_errors == ["bad: Gmail API error"]
    assert classifier.classify.call_count == 2


def test_fetch_recent_failure_propagates(sample_rule: RuleConfig) -> None:
    provider = MagicMock()
    provider.fetch_recent.side_effect = RuntimeError("Gmail API error listing messages")
    with pytest.raises(RuntimeError):
        run_playground(provider=provider, classifier=None, log=None, rule=sample_rule, days=14, limit=10)


def test_prompts_only_makes_no_classify_calls(sample_rule: RuleConfig) -> None:
    provider = _provider(["m1", "m2"])
    classifier = MagicMock()

    result = run_playground(provider=provider, classifier=None, log=None, rule=sample_rule, days=14, limit=10)

    classifier.classify.assert_not_called()
    assert len(result.items) == 2
    assert all(i.verdict is None and i.error is None and i.changed is None for i in result.items)
    assert all(i.system_prompt and i.user_message for i in result.items)


def test_changed_against_logged_entries(sample_rule: RuleConfig, tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _record(log, "same", "relevant")
    _record(log, "flipped", "relevant")
    provider = _provider(["same", "flipped", "new"])
    classifier = MagicMock()
    classifier.classify.side_effect = lambda email, rule: Verdict(
        verdict="irrelevant" if email.message_id == "flipped" else "relevant", confidence="high", reason="r",
    )

    result = run_playground(provider=provider, classifier=classifier, log=log, rule=sample_rule, days=14, limit=10)
    by_id = {i.email.message_id: i for i in result.items}

    assert by_id["same"].logged.verdict == "relevant"
    assert by_id["same"].changed is False
    assert by_id["flipped"].changed is True
    assert by_id["new"].logged is None
    assert by_id["new"].changed is None


def test_changed_is_none_without_new_verdict(relevant_email: Email) -> None:
    logged = MagicMock(verdict="relevant")
    item = PlaygroundItem(
        email=relevant_email, processed_email=relevant_email, system_prompt="s", user_message="u", logged=logged,
    )
    assert item.changed is None
    item.error = "boom"
    assert item.changed is None


def test_never_modifies_gmail_or_the_decision_log(sample_rule: RuleConfig, tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _record(log, "m1", "relevant")
    before = _row_count(tmp_db)
    provider = _provider(["m1", "m2"])
    classifier = MagicMock()
    classifier.classify.return_value = Verdict(verdict="irrelevant", confidence="high", reason="r")

    run_playground(provider=provider, classifier=classifier, log=log, rule=sample_rule, days=14, limit=10)

    provider.apply_label.assert_not_called()
    provider.archive.assert_not_called()
    provider.move_to_inbox.assert_not_called()
    provider.fetch_unread.assert_not_called()
    assert _row_count(tmp_db) == before
    assert not log.is_processed("m2")


# ------------------------------------------------------------------ trial_rule

def test_trial_rule_replaces_only_interests_and_tolerance(sample_rule_archive: RuleConfig) -> None:
    trial = trial_rule(sample_rule_archive, "Only Rust.", "conservative")
    assert trial.interests == "Only Rust."
    assert trial.tolerance == "conservative"
    assert trial == sample_rule_archive.model_copy(update={"interests": "Only Rust.", "tolerance": "conservative"})
    assert sample_rule_archive.interests == "Distributed systems and database internals."  # original untouched


@pytest.mark.parametrize("interests", ["", "   \n"])
def test_trial_rule_rejects_empty_interests(sample_rule: RuleConfig, interests: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        trial_rule(sample_rule, interests, "balanced")


def test_trial_rule_rejects_bad_tolerance(sample_rule: RuleConfig) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        trial_rule(sample_rule, "python", "reckless")
