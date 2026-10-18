from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from em_phi.cli import cli


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
email_provider:
  credentials_file: credentials.json
  token_file: token.json
decision_log:
  path: decisions.db
web:
  host: 127.0.0.1
  port: 8080
  auth_token: secret
rules:
  - email: newsletter@example.com
    name: Example Newsletter
    interests: Python releases
""")
    return config_path


def test_serve_configures_logging_before_starting_uvicorn(tmp_path: Path) -> None:
    """serve is the only long-running command; if it skips _configure_logging,
    every em_phi.* INFO log (scheduler start, processor summaries, Gmail
    actions) is silently dropped for the lifetime of the process — the root
    logger never gets a handler."""
    config_path = _write_config(tmp_path)
    runner = CliRunner()

    with patch("em_phi.cli._configure_logging") as mock_configure_logging, \
         patch("uvicorn.run") as mock_uvicorn_run:
        result = runner.invoke(cli, ["--config", str(config_path), "serve"])

    assert result.exit_code == 0, result.output
    mock_configure_logging.assert_called_once()
    mock_uvicorn_run.assert_called_once()


def test_log_action_filter_accepts_inbox_and_rejects_keep(tmp_path: Path) -> None:
    from em_phi.decision_log import DecisionLog

    config_path = _write_config(tmp_path)
    DecisionLog(tmp_path / "decisions.db")  # creates the db file
    runner = CliRunner()

    ok = runner.invoke(cli, ["--config", str(config_path), "log", "--action", "inbox"])
    assert ok.exit_code == 0, ok.output

    bad = runner.invoke(cli, ["--config", str(config_path), "log", "--action", "keep"])
    assert bad.exit_code == 2
    assert "Invalid value" in bad.output


def test_run_dry_run_output(tmp_path: Path, relevant_email, irrelevant_email,
                            relevant_verdict, irrelevant_verdict) -> None:
    from unittest.mock import MagicMock

    config_path = _write_config(tmp_path)
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001", "msg002"]
    provider.get_message.side_effect = {"msg001": relevant_email, "msg002": irrelevant_email}.__getitem__
    classifier = MagicMock()
    classifier.classify.side_effect = lambda email, rule: (
        relevant_verdict if email.message_id == "msg001" else irrelevant_verdict
    )
    runner = CliRunner()

    with patch("em_phi.cli._configure_logging"), \
         patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=classifier):
        result = runner.invoke(cli, ["--config", str(config_path), "run", "--dry-run"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "[DRY RUN] No changes will be made to Gmail or the decision log."
    assert lines[1] == ""
    assert lines[2] == "Processing ['newsletter@example.com'] (\"Example Newsletter\")..."
    assert lines[3].startswith("  [DRY RUN] [relevant ")
    assert lines[4].startswith("  [DRY RUN] [irrelevant ")
    assert lines[5] == "  Done: 2 processed, 1 relevant, 1 irrelevant, 0 skipped"
    assert lines[6] == ""
    assert lines[7] == "Run complete: 2 processed, 1 relevant, 1 irrelevant, 0 skipped"
    assert len(lines) == 8


def _write_multi_address_config(tmp_path: Path, auth_token: str = "a-long-enough-secret") -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
email_provider:
  credentials_file: credentials.json
  token_file: token.json
decision_log:
  path: decisions.db
web:
  auth_token: {auth_token}
rules:
  - email: [a@x.com, b@x.com]
    name: Python Weekly
    interests: Python releases
  - email: c@y.com
    name: Other News
    interests: Other things
""")
    return config_path


def _record(log, message_id: str, sender: str, rule_name: str) -> None:
    from datetime import datetime, timezone

    from em_phi.models import Verdict

    log.record(
        message_id=message_id,
        sender=sender,
        subject=f"Subject {message_id}",
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        verdict=Verdict(verdict="relevant", confidence="high", reason="r"),
        action_taken="label",
        rule_name=rule_name,
    )


def test_log_rule_option_accepts_rule_name_or_address(tmp_path: Path) -> None:
    from em_phi.decision_log import DecisionLog

    config_path = _write_multi_address_config(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    _record(log, "m1", "a@x.com", "Python Weekly")
    _record(log, "m2", "b@x.com", "Python Weekly")
    _record(log, "m3", "c@y.com", "Other News")
    runner = CliRunner()

    by_name = runner.invoke(cli, ["--config", str(config_path), "log", "--rule", "Python Weekly"])
    assert by_name.exit_code == 0, by_name.output
    assert "Subject m1" in by_name.output and "Subject m2" in by_name.output
    assert "Subject m3" not in by_name.output
    assert "Matching filter: 2" in by_name.output

    by_address = runner.invoke(cli, ["--config", str(config_path), "log", "--rule", "c@y.com"])
    assert by_address.exit_code == 0, by_address.output
    assert "Subject m3" in by_address.output
    assert "Subject m1" not in by_address.output


def test_log_backfills_rule_names_for_old_rows(tmp_path: Path) -> None:
    import sqlite3

    from em_phi.decision_log import DecisionLog

    config_path = _write_multi_address_config(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    _record(log, "m1", "b@x.com", "Python Weekly")
    conn = sqlite3.connect(tmp_path / "decisions.db")
    conn.execute("UPDATE decisions SET rule_name = NULL")
    conn.commit()
    conn.close()

    result = CliRunner().invoke(cli, ["--config", str(config_path), "log", "--rule", "Python Weekly"])

    assert result.exit_code == 0, result.output
    assert "Subject m1" in result.output


def test_serve_warns_about_default_auth_token(tmp_path: Path) -> None:
    config_path = _write_multi_address_config(tmp_path, auth_token="change-me")

    with patch("em_phi.cli._configure_logging"), patch("uvicorn.run") as mock_uvicorn_run:
        result = CliRunner().invoke(cli, ["--config", str(config_path), "serve"])

    assert result.exit_code == 0, result.output
    assert "WARNING: web.auth_token is the default or too short" in result.output
    mock_uvicorn_run.assert_called_once()  # a warning, not a refusal


def test_serve_warns_about_short_auth_token(tmp_path: Path) -> None:
    config_path = _write_multi_address_config(tmp_path, auth_token="short")

    with patch("em_phi.cli._configure_logging"), patch("uvicorn.run"):
        result = CliRunner().invoke(cli, ["--config", str(config_path), "serve"])

    assert "WARNING: web.auth_token" in result.output


def test_serve_is_quiet_about_a_strong_auth_token(tmp_path: Path) -> None:
    config_path = _write_multi_address_config(tmp_path)

    with patch("em_phi.cli._configure_logging"), patch("uvicorn.run"):
        result = CliRunner().invoke(cli, ["--config", str(config_path), "serve"])

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output


# ------------------------------------------------------------------ run history


def test_runs_without_a_database_says_so_and_does_not_create_it(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    result = CliRunner().invoke(cli, ["--config", str(config_path), "runs"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "No runs recorded yet."
    assert not (tmp_path / "decisions.db").exists()


def test_run_is_recorded_and_listed_by_runs(tmp_path: Path, relevant_email, relevant_verdict) -> None:
    from unittest.mock import MagicMock

    config_path = _write_config(tmp_path)
    provider = MagicMock()
    provider.fetch_unread.return_value = ["msg001"]
    provider.get_message.return_value = relevant_email
    classifier = MagicMock()
    classifier.classify.return_value = relevant_verdict
    runner = CliRunner()

    with patch("em_phi.cli._configure_logging"), \
         patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=classifier):
        run = runner.invoke(cli, ["--config", str(config_path), "run", "--dry-run"])
    assert run.exit_code == 0, run.output

    result = runner.invoke(cli, ["--config", str(config_path), "runs"])

    assert result.exit_code == 0, result.output
    header, rule, row = result.output.splitlines()
    assert header.startswith("Started (UTC)")
    assert set(rule) == {"-"}
    fields = row.split()
    assert fields[2:5] == ["cli", "dry", "all"]
    assert fields[5:10] == ["1", "1", "0", "0", "0"]
    assert fields[10] == "ok"


def test_failed_run_is_recorded_with_its_errors(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from em_phi.processor import RuleResult, RunSummary
    from em_phi.run_log import RunLog

    config_path = _write_config(tmp_path)
    provider = MagicMock()
    provider.authenticate.side_effect = RuntimeError("Token not found: token.json\nSee docs/gmail-setup.md")

    with patch("em_phi.cli._configure_logging"), \
         patch("em_phi.cli._build_provider", return_value=provider):
        run = CliRunner().invoke(cli, ["--config", str(config_path), "run"])
    assert run.exit_code == 1
    assert "Token not found" in run.output

    log = RunLog(tmp_path / "decisions.db")
    with_errors = log.start(trigger="scheduled", dry_run=False, rule_filter="newsletter@example.com")
    log.finish(
        with_errors,
        RunSummary(results=[RuleResult(rule_email="newsletter@example.com", errors=7)]),
        [f"processing message m{i}: boom" for i in range(7)],
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "runs", "--limit", "5"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "scheduled" in lines[2] and "newsletter@example.com" in lines[2] and "errors" in lines[2]
    assert lines[3:9] == [f"    Error: processing message m{i}: boom" for i in range(5)] + ["    … and 2 more"]
    assert "cli" in lines[9] and "failed" in lines[9]
    assert lines[10] == "    Failure: Token not found: token.json"
    assert lines[11] == "             See docs/gmail-setup.md"


def test_runs_limit_is_bounded(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", str(config_path), "runs", "--limit", "201"])
    assert result.exit_code == 2


def test_check_config_warns_about_small_max_tokens(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(config_path.read_text() + "llm:\n  max_tokens: 256\n")
    result = CliRunner().invoke(cli, ["--config", str(config_path), "check-config"])
    assert result.exit_code == 0, result.output
    assert "WARNING: llm.max_tokens is 256; em-phi uses 1024 instead" in result.output


def test_check_config_is_quiet_about_default_max_tokens(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--config", str(_write_config(tmp_path)), "check-config"])
    assert result.exit_code == 0, result.output
    assert "max_tokens" not in result.output
