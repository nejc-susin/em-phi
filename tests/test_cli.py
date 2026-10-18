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
