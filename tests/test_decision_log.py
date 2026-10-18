import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from em_phi.config import RuleConfig
from em_phi.decision_log import _SCHEMA, DecisionLog
from em_phi.models import Verdict

_FMT = "%Y-%m-%d %H:%M:%S"
_MIGRATION_BACKUP = "decisions_pre_action_taken_migration"


def _add(
    log: DecisionLog, message_id: str, sender: str = "a@example.com", rule_name: str = "Example Newsletter",
) -> None:
    log.record(
        message_id=message_id,
        sender=sender,
        subject="s",
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        verdict=Verdict(verdict="relevant", confidence="high", reason="r"),
        action_taken="label",
        rule_name=rule_name,
    )


def _set_processed_at(path: Path, message_id: str, value: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("UPDATE decisions SET processed_at = ? WHERE message_id = ?", (value, message_id))
    conn.commit()
    conn.close()


def test_days_filter_includes_rows_inside_cutoff_day(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "recent")
    _add(log, "old")
    now = datetime.now(tz=timezone.utc)
    _set_processed_at(tmp_db, "recent", (now - timedelta(hours=23)).strftime(_FMT))
    _set_processed_at(tmp_db, "old", (now - timedelta(hours=25)).strftime(_FMT))

    assert log.count(days=1) == {"relevant": 1}
    assert [e.message_id for e in log.query(days=1)] == ["recent"]


def test_rule_filter_matches_domain_rules(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "m1", "A <a@example.com>")
    _add(log, "m2", "b@Example.com")
    _add(log, "m3", "c@notexample.com")
    _add(log, "m4", "d@example.com.evil.org")

    assert {e.message_id for e in log.query(rule_email="example.com")} == {"m1", "m2"}
    assert {e.message_id for e in log.query(rule_email="a@example.com")} == {"m1"}
    assert {e.message_id for e in log.query(rule_email="c@notexample.com")} == {"m3"}


def test_query_order_is_stable_for_identical_processed_at(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "first")
    _add(log, "second")
    _set_processed_at(tmp_db, "first", "2026-10-18 10:00:00")
    _set_processed_at(tmp_db, "second", "2026-10-18 10:00:00")

    entries = log.query()
    assert [e.message_id for e in entries] == ["second", "first"]
    assert entries[0].id > entries[1].id


_OLD_SCHEMA = _SCHEMA.replace(
    "action_taken  TEXT NOT NULL,",
    "action_taken  TEXT NOT NULL CHECK(action_taken IN ('label','archive')),",
)


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (message_id, sender, subject, received_at, verdict, confidence, reason, action_taken)"
        " VALUES ('m1', 'a@example.com', 's', '2026-05-01', 'relevant', 'high', 'r', 'label')"
    )
    conn.commit()
    conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return names


def _decisions_sql(path: Path) -> str:
    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='decisions'").fetchone()[0]
    conn.close()
    return sql


def test_migration_drops_check_and_keeps_rows(tmp_db: Path) -> None:
    _make_old_db(tmp_db)
    assert "CHECK(action_taken" in _decisions_sql(tmp_db)

    log = DecisionLog(tmp_db)

    assert [e.message_id for e in log.query()] == ["m1"]
    assert "CHECK(action_taken" not in _decisions_sql(tmp_db)
    assert _MIGRATION_BACKUP not in _table_names(tmp_db)


def test_failed_migration_is_rolled_back(tmp_db: Path) -> None:
    _make_old_db(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute(f"CREATE TABLE {_MIGRATION_BACKUP} (x)")  # makes the RENAME fail
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError):
        DecisionLog(tmp_db)

    assert "CHECK(action_taken" in _decisions_sql(tmp_db)
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT message_id FROM decisions").fetchall() == [("m1",)]
    conn.close()


def test_failure_midway_through_migration_is_rolled_back(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fails after the RENAME has already run, so rollback must undo real work.
    _make_old_db(tmp_db)
    monkeypatch.setattr("em_phi.decision_log._SCHEMA", _SCHEMA + "\nSELECT * FROM no_such_table;\n")

    with pytest.raises(sqlite3.OperationalError):
        DecisionLog(tmp_db)

    assert _table_names(tmp_db) >= {"decisions"}
    assert _MIGRATION_BACKUP not in _table_names(tmp_db)
    assert "CHECK(action_taken" in _decisions_sql(tmp_db)
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT message_id FROM decisions").fetchall() == [("m1",)]
    conn.close()


def _rule(name: str, email: str | list[str]) -> RuleConfig:
    return RuleConfig(email=email, name=name, interests="anything")


def _rule_names(log: DecisionLog) -> dict[str, str | None]:
    return {e.message_id: e.rule_name for e in log.query(limit=100)}


def test_record_stores_rule_name(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "m1", rule_name="Python Weekly")

    assert [e.rule_name for e in log.query()] == ["Python Weekly"]


def test_query_and_count_filter_by_rule_name(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "m1", "a@example.com", rule_name="Python Weekly")
    _add(log, "m2", "b@example.com", rule_name="Python Weekly")
    _add(log, "m3", "c@example.com", rule_name="Other")

    assert {e.message_id for e in log.query(rule_name="Python Weekly")} == {"m1", "m2"}
    assert log.count(rule_name="Python Weekly") == {"relevant": 2}
    assert log.count(rule_name="Other") == {"relevant": 1}
    assert log.query(rule_name="Missing") == []


def test_plain_db_without_rule_name_column_is_migrated(tmp_db: Path) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.executescript(
        """
        CREATE TABLE decisions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id    TEXT UNIQUE NOT NULL,
            sender        TEXT NOT NULL,
            subject       TEXT NOT NULL,
            received_at   TEXT NOT NULL,
            verdict       TEXT NOT NULL CHECK(verdict IN ('relevant', 'irrelevant')),
            confidence    TEXT NOT NULL CHECK(confidence IN ('high', 'medium', 'low')),
            reason        TEXT NOT NULL,
            action_taken  TEXT NOT NULL,
            processed_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_sender ON decisions(sender);
        CREATE INDEX idx_processed_at ON decisions(processed_at);
        """
    )
    conn.execute(
        "INSERT INTO decisions (message_id, sender, subject, received_at, verdict, confidence, reason, action_taken)"
        " VALUES ('old', 'a@example.com', 's', '2026-05-01', 'relevant', 'high', 'r', 'label')"
    )
    conn.commit()
    conn.close()

    log = DecisionLog(tmp_db)

    conn = sqlite3.connect(tmp_db)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "rule_name" in columns
    assert "idx_rule_name" in indexes
    assert _rule_names(log) == {"old": None}

    _add(log, "new", rule_name="Python Weekly")
    assert _rule_names(log) == {"old": None, "new": "Python Weekly"}

    DecisionLog(tmp_db)  # reopening an already-migrated db is a no-op
    assert len(log.query(limit=100)) == 2


def test_check_constraint_migration_also_adds_rule_name(tmp_db: Path) -> None:
    _make_old_db(tmp_db)

    log = DecisionLog(tmp_db)

    assert _rule_names(log) == {"m1": None}
    _add(log, "m2", rule_name="Python Weekly")
    assert _rule_names(log) == {"m1": None, "m2": "Python Weekly"}


def test_backfill_matches_exact_address_and_display_name_sender(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add_unnamed(tmp_db, "m1", "Weekly <News@Example.com>")
    _add_unnamed(tmp_db, "m2", "other@example.com")

    updated = log.backfill_rule_names([_rule("Weekly", "news@example.com")])

    assert updated == 1
    assert _rule_names(log) == {"m1": "Weekly", "m2": None}


def test_backfill_matches_domain_rules_on_whole_domain_only(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add_unnamed(tmp_db, "m1", "a@example.com")
    _add_unnamed(tmp_db, "m2", "B <b@Example.com>")
    _add_unnamed(tmp_db, "m3", "c@notexample.com")
    _add_unnamed(tmp_db, "m4", "d@example.com.evil.org")

    updated = log.backfill_rule_names([_rule("Example", "Example.com")])

    assert updated == 2
    assert _rule_names(log) == {"m1": "Example", "m2": "Example", "m3": None, "m4": None}


def test_backfill_first_matching_rule_wins(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add_unnamed(tmp_db, "m1", "a@example.com")

    log.backfill_rule_names([_rule("Specific", "a@example.com"), _rule("Domain", "example.com")])

    assert _rule_names(log) == {"m1": "Specific"}


def test_backfill_leaves_named_rows_alone_and_is_idempotent(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "named", "a@example.com", rule_name="Recorded")
    _add_unnamed(tmp_db, "m1", "a@example.com")
    _add_unnamed(tmp_db, "m2", "nobody@else.org")
    rules = [_rule("Example", ["a@example.com", "b@example.com"])]

    assert log.backfill_rule_names(rules) == 1
    assert log.backfill_rule_names(rules) == 0
    assert _rule_names(log) == {"named": "Recorded", "m1": "Example", "m2": None}


def _add_unnamed(path: Path, message_id: str, sender: str) -> None:
    """Insert a row the way a pre-rule_name version would have: rule_name NULL."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO decisions (message_id, sender, subject, received_at, verdict, confidence, reason, action_taken)"
        " VALUES (?, ?, 's', '2026-05-01', 'relevant', 'high', 'r', 'label')",
        (message_id, sender),
    )
    conn.commit()
    conn.close()


def test_get_returns_the_logged_entry(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "m1", sender="Name <a@example.com>")
    entry = log.get("m1")
    assert entry is not None
    assert entry.message_id == "m1"
    assert entry.sender == "Name <a@example.com>"
    assert entry.verdict == "relevant"
    assert entry.confidence == "high"
    assert entry.rule_name == "Example Newsletter"


def test_get_returns_none_for_unknown_message(tmp_db: Path) -> None:
    log = DecisionLog(tmp_db)
    _add(log, "m1")
    assert log.get("nope") is None
