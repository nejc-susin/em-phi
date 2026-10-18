from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from fastapi.testclient import TestClient

from em_phi.config import load_config
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.web.app import _session_value, create_app

CONFIG_YAML = """\
email_provider:
  name: gmail
  credentials_file: credentials.json
  token_file: token.json
decision_log:
  path: decisions.db
web:
  auth_token: secret
rules:
  - email: [news1@example.com, news2@example.com]
    name: Multi News
    interests: python releases
    tolerance: conservative
    action: archive
  - email: digest@example.com
    name: Tech Digest
    interests: distributed systems
    tolerance: aggressive
    action: inbox
"""


def _setup(tmp_path: Path) -> tuple[TestClient, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML)
    app = create_app(load_config(config_path), config_path)
    client = TestClient(app)
    client.cookies.set("em_phi_auth", _session_value("secret"))
    return client, config_path


def _form(**overrides: str) -> dict[str, str]:
    form = {
        "rule_index": "0",
        "rule_name": "Multi News",
        "interests": "Only the Python security advisories.",
        "tolerance": "aggressive",
        "days": "14",
        "limit": "10",
    }
    form.update(overrides)
    return form


def _email(message_id: str, subject: str) -> Email:
    return Email(
        message_id=message_id,
        sender="news1@example.com",
        subject=subject,
        body="body",
        received_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
    )


def _provider(emails: list[Email]) -> MagicMock:
    by_id = {e.message_id: e for e in emails}
    provider = MagicMock()
    provider.fetch_recent.return_value = list(by_id)
    provider.get_message.side_effect = lambda mid: by_id[mid]
    return provider


def _log_decision(tmp_path: Path, message_id: str, verdict: str) -> None:
    DecisionLog(tmp_path / "decisions.db").record(
        message_id=message_id,
        sender="news1@example.com",
        subject="s",
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        verdict=Verdict(verdict=verdict, confidence="medium", reason="logged reason"),  # type: ignore[arg-type]
        action_taken="label",
        rule_name="Multi News",
    )


# ------------------------------------------------------------------ GET


def test_get_renders_form_prefilled_for_first_rule(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.get("/playground")
    assert response.status_code == 200
    assert "python releases</textarea>" in response.text
    assert '<option value="0" selected>' in response.text
    assert '<option value="conservative" selected>' in response.text
    assert 'name="rule_name" value="Multi News"' in response.text
    assert 'name="days" type="number" min="1" max="90" value="14"' in response.text
    assert 'name="limit" type="number" min="1" max="20" value="10"' in response.text


def test_get_with_rule_param_prefills_that_rule(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.get("/playground", params={"rule": "1"})
    assert "distributed systems</textarea>" in response.text
    assert '<option value="1" selected>' in response.text
    assert '<option value="aggressive" selected>' in response.text
    assert 'name="rule_name" value="Tech Digest"' in response.text


def test_get_with_invalid_rule_param_falls_back_to_first_rule(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    for value in ("7", "-1", "abc", ""):
        response = client.get("/playground", params={"rule": value})
        assert response.status_code == 200
        assert "python releases</textarea>" in response.text


def test_rule_dropdown_shows_name_and_addresses(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.get("/playground")
    assert "Multi News &mdash; news1@example.com, news2@example.com" in response.text
    assert "Tech Digest &mdash; digest@example.com" in response.text


def test_nav_links_to_playground(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.get("/playground")
    assert 'href="/playground"' in response.text
    assert 'href="/debug"' not in response.text


def test_saved_banner(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    assert "Profile saved" in client.get("/playground", params={"saved": "1"}).text
    assert "Profile saved" not in client.get("/playground").text


def test_debug_redirects_to_playground(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.get("/debug", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/playground"


# ------------------------------------------------------------------ POST /playground/test


def test_test_shows_results_with_changed_row_and_keeps_edits(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    original_yaml = config_path.read_text()
    _log_decision(tmp_path, "m1", "relevant")
    _log_decision(tmp_path, "m2", "relevant")
    provider = _provider([_email("m1", "Flipped <b>issue</b>"), _email("m2", "Same issue"), _email("m3", "New issue")])
    classifier = MagicMock()
    classifier.classify.side_effect = lambda email, rule: Verdict(
        verdict="irrelevant" if email.message_id == "m1" else "relevant", confidence="high", reason=f"new {rule.tolerance}",
    )

    with patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=classifier):
        response = client.post("/playground/test", data=_form())

    assert response.status_code == 200
    text = response.text
    # Classified with the edited profile, not the saved one
    rule_arg = classifier.classify.call_args.args[1]
    assert rule_arg.interests == "Only the Python security advisories."
    assert rule_arg.tolerance == "aggressive"
    # Edited values preserved in the form
    assert "Only the Python security advisories.</textarea>" in text
    assert '<option value="aggressive" selected>' in text
    # Summary, changed highlighting, not-processed row, autoescaped subject
    assert "3 emails &middot; 1 would change &middot; 2 relevant / 1 irrelevant (new)" in text
    assert text.count('data-changed="1"') == 1
    assert "bg-amber-50" in text
    assert "not processed" in text
    assert "relevant · medium" in text
    assert "Flipped &lt;b&gt;issue&lt;/b&gt;" in text
    assert "https://mail.google.com/mail/u/0/#all/m1" in text
    assert "System prompt" in text and "Only the Python security advisories." in text
    # Nothing written anywhere
    provider.apply_label.assert_not_called()
    provider.archive.assert_not_called()
    provider.move_to_inbox.assert_not_called()
    assert config_path.read_text() == original_yaml
    assert not DecisionLog(tmp_path / "decisions.db").is_processed("m3")


def test_test_without_decision_db_does_not_create_it(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    provider = _provider([_email("m1", "Issue")])
    classifier = MagicMock()
    classifier.classify.return_value = Verdict(verdict="relevant", confidence="high", reason="r")

    with patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=classifier):
        response = client.post("/playground/test", data=_form())

    assert response.status_code == 200
    assert "not processed" in response.text
    assert not (tmp_path / "decisions.db").exists()


def test_prompts_only_never_builds_the_classifier(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    provider = _provider([_email("m1", "Issue one"), _email("m2", "Issue two")])
    build_classifier = MagicMock()

    with patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", build_classifier):
        response = client.post("/playground/test", data=_form(prompts_only="on"))

    assert response.status_code == 200
    build_classifier.assert_not_called()
    assert "2 prompts built (no Claude calls)" in response.text
    assert 'name="prompts_only" type="checkbox" checked' in response.text
    assert "Only the Python security advisories." in response.text


def test_missing_api_key_shows_banner(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    build_provider = MagicMock()

    with patch("em_phi.cli._build_provider", build_provider), \
         patch("em_phi.cli._build_classifier", side_effect=RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")):
        response = client.post("/playground/test", data=_form())

    assert "ANTHROPIC_API_KEY environment variable is not set." in response.text
    assert "Prompts only" in response.text
    assert "Only the Python security advisories.</textarea>" in response.text
    build_provider.assert_not_called()


def test_provider_error_shows_banner(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    provider = MagicMock()
    provider.authenticate.side_effect = RuntimeError("Token not found: token.json")

    with patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=MagicMock()):
        response = client.post("/playground/test", data=_form())

    assert "Token not found: token.json" in response.text


def test_stale_rule_name_shows_stale_banner(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    build_provider = MagicMock()

    with patch("em_phi.cli._build_provider", build_provider):
        response = client.post("/playground/test", data=_form(rule_name="Old Name"))
        out_of_range = client.post("/playground/test", data=_form(rule_index="5"))

    assert response.status_code == 422
    assert "The rules changed since this page was loaded — reload and try again." in response.text
    assert "Only the Python security advisories.</textarea>" in response.text
    assert "The rules changed since this page was loaded" in out_of_range.text
    build_provider.assert_not_called()


def test_test_with_empty_interests_shows_error(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    build_provider = MagicMock()
    with patch("em_phi.cli._build_provider", build_provider):
        response = client.post("/playground/test", data=_form(interests="   "))
    assert response.status_code == 422
    assert "must not be empty" in response.text
    build_provider.assert_not_called()


def test_days_and_limit_are_clamped(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    provider = _provider([])

    def post(days: str, limit: str) -> tuple[int, int]:
        provider.fetch_recent.reset_mock()
        with patch("em_phi.cli._build_provider", return_value=provider):
            response = client.post("/playground/test", data=_form(days=days, limit=limit, prompts_only="on"))
        assert response.status_code == 200
        _, got_days, got_limit = provider.fetch_recent.call_args.args
        return got_days, got_limit

    assert post("500", "500") == (90, 20)
    assert post("0", "-3") == (1, 1)
    assert post("", "") == (14, 10)
    assert post("abc", "1.5") == (14, 10)
    assert post("30", "5") == (30, 5)


def test_empty_result_message(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    with patch("em_phi.cli._build_provider", return_value=_provider([])):
        response = client.post("/playground/test", data=_form(days="3", prompts_only="on"))
    assert "No emails from this rule in the last 3 days." in response.text


def test_fetch_errors_are_listed(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    provider = MagicMock()
    provider.fetch_recent.return_value = ["bad"]
    provider.get_message.side_effect = RuntimeError("Gmail API error fetching message bad")
    with patch("em_phi.cli._build_provider", return_value=provider):
        response = client.post("/playground/test", data=_form(prompts_only="on"))
    assert "bad: Gmail API error fetching message bad" in response.text


def test_cross_site_post_is_blocked(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    response = client.post("/playground/save", data=_form(), headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403


# ------------------------------------------------------------------ POST /playground/save


def test_save_writes_only_interests_and_tolerance(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)

    response = client.post(
        "/playground/save",
        data=_form(rule_index="1", rule_name="Tech Digest", interests="  Consensus papers only.\n", tolerance="balanced"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/playground?rule=1&saved=1"
    rules = yaml.safe_load(config_path.read_text())["rules"]
    assert rules[0] == {
        "email": ["news1@example.com", "news2@example.com"],
        "name": "Multi News",
        "interests": "python releases",
        "tolerance": "conservative",
        "action": "archive",
    }
    assert rules[1] == {
        "email": "digest@example.com",
        "name": "Tech Digest",
        "interests": "Consensus papers only.",
        "tolerance": "balanced",
        "action": "inbox",
    }
    # In-memory config reloaded too
    page = client.get("/playground", params={"rule": "1"}).text
    assert "Consensus papers only.</textarea>" in page


def test_save_with_empty_interests_shows_error_and_does_not_write(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_text()

    response = client.post("/playground/save", data=_form(interests=" "), follow_redirects=False)

    assert response.status_code == 422
    assert "must not be empty" in response.text
    assert config_path.read_text() == before


def test_save_with_bad_tolerance_shows_error_and_keeps_edits(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_text()

    response = client.post("/playground/save", data=_form(tolerance="reckless"), follow_redirects=False)

    assert response.status_code == 422
    assert "tolerance" in response.text
    assert "Only the Python security advisories.</textarea>" in response.text
    assert config_path.read_text() == before


def test_save_with_stale_rule_name_does_not_write(tmp_path: Path) -> None:
    client, config_path = _setup(tmp_path)
    before = config_path.read_text()

    response = client.post("/playground/save", data=_form(rule_name="Tech Digest"), follow_redirects=False)

    assert response.status_code == 422
    assert "The rules changed since this page was loaded" in response.text
    assert config_path.read_text() == before
