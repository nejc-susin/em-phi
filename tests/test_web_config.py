import asyncio
from pathlib import Path

import pytest
import yaml
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.testclient import TestClient
from pydantic import ValidationError

from em_phi.config import AppConfig, ScheduleConfig, load_config
from em_phi.web.app import _session_value, create_app
from em_phi.web.scheduler import EmPhiScheduler

CONFIG_YAML = """\
email_provider:
  name: gmail
  credentials_file: credentials.json
  token_file: token.json
  imap_host: example.org
llm:
  name: claude
  model: claude-haiku-5-5
  max_tokens: 1024
decision_log:
  path: decisions.db
schedule:
  enabled: false
  interval_hours: 6
web:
  auth_token: secret
rules:
  - email: newsletter@example.com
    name: Example Newsletter
    interests: python
"""


def _setup(tmp_path: Path, yaml_text: str = CONFIG_YAML) -> tuple[TestClient, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)
    config_path.chmod(0o600)
    app = create_app(load_config(config_path), config_path)
    client = TestClient(app)
    client.cookies.set("em_phi_auth", _session_value("secret"))
    return client, config_path


def _settings_form(**overrides: str) -> dict[str, str]:
    form = {
        "model": "claude-haiku-5-5",
        "max_tokens": "2048",
        "labels_relevant": "EmPhi/Keep",
        "labels_irrelevant": "EmPhi/Skip",
        "decision_log_path": "decisions.db",
        "log_level": "INFO",
        "fetch_label": "",
    }
    form.update(overrides)
    return form


def test_save_settings_preserves_llm_name_extras_and_relative_paths(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)

    response = client.post("/config/settings", data=_settings_form(), follow_redirects=False)
    assert response.status_code == 303

    data = yaml.safe_load(config_path.read_text())
    assert data["llm"]["name"] == "claude"
    assert data["llm"]["max_tokens"] == 2048
    assert data["labels"]["relevant"] == "EmPhi/Keep"
    assert data["email_provider"]["imap_host"] == "example.org"
    assert data["email_provider"]["credentials_file"] == "credentials.json"
    assert data["email_provider"]["token_file"] == "token.json"
    assert data["decision_log"]["path"] == "decisions.db"
    assert data["web"]["auth_token"] == "secret"
    assert config_path.stat().st_mode & 0o777 == 0o600
    # No temp files left behind
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_save_settings_persists_read_mail_options(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)

    # An unchecked checkbox is simply absent from the submitted form.
    response = client.post(
        "/config/settings", data=_settings_form(lookback_days="7"), follow_redirects=False
    )
    assert response.status_code == 303

    ep = yaml.safe_load(config_path.read_text())["email_provider"]
    assert ep["unread_only"] is False
    assert ep["lookback_days"] == 7
    reloaded = load_config(config_path).email_provider
    assert (reloaded.unread_only, reloaded.lookback_days) == (False, 7)

    page = client.get("/config").text
    assert 'name="lookback_days"' in page and 'value="7"' in page
    assert "checked" not in page.split('id="unread_only"')[1].split(">")[0]


def test_save_settings_does_not_write_default_read_mail_options(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)

    response = client.post(
        "/config/settings",
        data=_settings_form(unread_only="on", lookback_days="3"),
        follow_redirects=False,
    )
    assert response.status_code == 303

    ep = yaml.safe_load(config_path.read_text())["email_provider"]
    assert "unread_only" not in ep
    assert "lookback_days" not in ep
    assert "account" not in ep


def test_save_settings_preserves_account(tmp_path: Path) -> None:
    client, config_path = _setup(
        tmp_path, CONFIG_YAML.replace("  imap_host: example.org\n", "  account: me@example.com\n")
    )

    response = client.post(
        "/config/settings", data=_settings_form(unread_only="on"), follow_redirects=False
    )
    assert response.status_code == 303
    assert yaml.safe_load(config_path.read_text())["email_provider"]["account"] == "me@example.com"


@pytest.mark.parametrize("days", ["0", "31", "abc"])
def test_invalid_lookback_days_shows_error_banner(tmp_path: Path, days: str) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_bytes()

    response = client.post(
        "/config/settings", data=_settings_form(lookback_days=days), follow_redirects=False
    )
    assert response.status_code == 422
    assert "bg-red-50" in response.text
    assert config_path.read_bytes() == before


def test_save_settings_writes_outside_paths_absolute(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    outside = tmp_path.parent / "elsewhere.db"

    response = client.post(
        "/config/settings", data=_settings_form(decision_log_path=str(outside)), follow_redirects=False
    )
    assert response.status_code == 303
    assert yaml.safe_load(config_path.read_text())["decision_log"]["path"] == str(outside)


def test_invalid_cron_is_rejected_without_touching_disk_or_memory(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_bytes()

    response = client.post(
        "/config/schedule",
        data={"enabled": "on", "interval_hours": "3", "cron": "not a cron"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "invalid cron expression" in response.text
    assert config_path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]

    # In-memory schedule unchanged: the config page still shows the old values
    page = client.get("/config").text
    assert 'value="6"' in page
    assert "not a cron" not in page


def test_bad_max_tokens_does_not_apply_other_settings(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_bytes()

    response = client.post(
        "/config/settings", data=_settings_form(max_tokens="abc"), follow_redirects=False
    )
    assert response.status_code == 422
    assert config_path.read_bytes() == before

    page = client.get("/config").text
    assert "EmPhi/Keep" not in page
    assert "EmPhi/Relevant" in page


def test_edit_rule_accepts_comma_separated_emails(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)

    response = client.post(
        "/config/rule/0/edit",
        data={"email": "a@example.com, b@example.com", "name": "Both", "interests": "rust"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rule = yaml.safe_load(config_path.read_text())["rules"][0]
    assert rule["email"] == ["a@example.com", "b@example.com"]


def test_deleting_last_rule_is_rejected(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_bytes()

    response = client.post("/config/rule/0/delete", follow_redirects=False)
    assert response.status_code == 422
    assert config_path.read_bytes() == before
    assert "newsletter@example.com" in client.get("/config").text


def test_schedule_config_validation() -> None:
    with pytest.raises(ValidationError, match="invalid cron expression"):
        ScheduleConfig(cron="not a cron")
    with pytest.raises(ValidationError):
        ScheduleConfig(interval_hours=0)
    assert ScheduleConfig(cron="   ").cron is None
    assert ScheduleConfig(cron=" 0 */6 * * * ").cron == "0 */6 * * *"
    assert ScheduleConfig(interval_hours=None).interval_hours is None


def _config_with_schedule(schedule: ScheduleConfig) -> AppConfig:
    return AppConfig.model_validate({
        "rules": [{"email": "a@example.com", "name": "A", "interests": "x"}],
    }).model_copy(update={"schedule": schedule})


def test_reschedule_replaces_job() -> None:
    async def run_fn() -> None:
        pass

    async def scenario() -> None:
        s = EmPhiScheduler()
        s.start(_config_with_schedule(ScheduleConfig(enabled=True, interval_hours=2)), run_fn)
        old = s._scheduler
        assert isinstance(old.get_job("em_phi_run").trigger, IntervalTrigger)

        s.reschedule(_config_with_schedule(ScheduleConfig(enabled=True, cron="0 * * * *")), run_fn)
        assert old.get_job("em_phi_run") is None
        await asyncio.sleep(0)  # AsyncIOScheduler shuts down via the event loop
        assert not old.running
        assert s._scheduler.running
        assert isinstance(s._scheduler.get_job("em_phi_run").trigger, CronTrigger)
        s.shutdown()

    asyncio.run(scenario())


def test_reschedule_failure_keeps_old_scheduler_running() -> None:
    async def run_fn() -> None:
        pass

    async def scenario() -> None:
        s = EmPhiScheduler()
        s.start(_config_with_schedule(ScheduleConfig(enabled=True, interval_hours=2)), run_fn)
        old = s._scheduler

        # model_construct bypasses the validator to simulate a bad trigger
        bad = ScheduleConfig.model_construct(enabled=True, interval_hours=6, cron="not a cron")
        with pytest.raises(ValueError):
            s.reschedule(_config_with_schedule(bad), run_fn)
        assert s._scheduler is old
        assert old.running
        assert old.get_job("em_phi_run") is not None
        s.shutdown()

    asyncio.run(scenario())
