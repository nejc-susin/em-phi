from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from em_phi.cli import cli


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
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
