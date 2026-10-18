import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from em_phi.decision_log import _SCHEMA, DecisionLog
from em_phi.models import Verdict

_FMT = "%Y-%m-%d %H:%M:%S"
_MIGRATION_BACKUP = "decisions_pre_action_taken_migration"


def _add(log: DecisionLog, message_id: str, sender: str = "a@example.com") -> None:
    log.record(
        message_id=message_id,
        sender=sender,
        subject="s",
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        verdict=Verdict(verdict="relevant", confidence="high", reason="r"),
        action_taken="label",
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
