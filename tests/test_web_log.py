from pathlib import Path

from fastapi.testclient import TestClient

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, RuleConfig, WebConfig
from em_phi.web.app import _session_value, create_app


def _make_client(tmp_path: Path) -> TestClient:
    config = AppConfig(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=Path("token.json"),
        ),
        decision_log=DecisionLogConfig(path=tmp_path / "decisions.db"),
        rules=[RuleConfig(email="newsletter@example.com", name="Example Newsletter", interests="python")],
        web=WebConfig(auth_token="secret"),
    )
    app = create_app(config, tmp_path / "config.yaml")
    client = TestClient(app)
    client.cookies.set("em_phi_auth", _session_value("secret"))
    return client


def test_log_page_blank_days_field_does_not_422(tmp_path: Path) -> None:
    """The Days filter input submits an empty string when left blank; that
    must not surface FastAPI's raw int-coercion validation error."""
    client = _make_client(tmp_path)

    response = client.get("/log", params={"days": ""})
    assert response.status_code == 200


def test_log_page_full_blank_filter_form_submission(tmp_path: Path) -> None:
    """Reproduces exactly what clicking Filter sends when only Limit has a value."""
    client = _make_client(tmp_path)

    response = client.get(
        "/log",
        params={"rule": "", "days": "", "verdict": "", "action": "", "search": "", "limit": "50", "offset": "0"},
    )
    assert response.status_code == 200


def test_log_page_garbage_limit_is_ignored_not_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.get("/log", params={"limit": "not-a-number"})
    assert response.status_code == 200


def test_log_page_filters_by_inbox_action_and_offers_only_recorded_actions(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from em_phi.decision_log import DecisionLog
    from em_phi.models import Verdict

    client = _make_client(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    for message_id, subject, action in (("m1", "Inbox subject", "inbox"), ("m2", "Label subject", "label")):
        log.record(
            message_id=message_id,
            sender="newsletter@example.com",
            subject=subject,
            received_at=datetime.now(timezone.utc),
            verdict=Verdict(verdict="relevant", confidence="high", reason="r"),
            action_taken=action,
            rule_name="Example Newsletter",
        )

    response = client.get("/log", params={"action": "inbox"})

    assert response.status_code == 200
    assert "Inbox subject" in response.text
    assert "Label subject" not in response.text
    assert '<option value="inbox" selected' in response.text
    assert 'value="keep"' not in response.text


def _make_multi_rule_client(tmp_path: Path) -> TestClient:
    config = AppConfig(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=Path("token.json"),
        ),
        decision_log=DecisionLogConfig(path=tmp_path / "decisions.db"),
        rules=[
            RuleConfig(email=["a@x.com", "b@x.com"], name="Python Weekly", interests="python"),
            RuleConfig(email="c@y.com", name="Other News", interests="other"),
        ],
        web=WebConfig(auth_token="secret"),
    )
    client = TestClient(create_app(config, tmp_path / "config.yaml"))
    client.cookies.set("em_phi_auth", _session_value("secret"))
    return client


def _record(log, message_id: str, sender: str, subject: str, rule_name: str) -> None:
    from datetime import datetime, timezone

    from em_phi.models import Verdict

    log.record(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_at=datetime.now(timezone.utc),
        verdict=Verdict(verdict="relevant", confidence="high", reason="r"),
        action_taken="label",
        rule_name=rule_name,
    )


def test_log_page_filters_by_rule_name_across_all_its_addresses(tmp_path: Path) -> None:
    from em_phi.decision_log import DecisionLog

    client = _make_multi_rule_client(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    _record(log, "m1", "a@x.com", "From first address", "Python Weekly")
    _record(log, "m2", "b@x.com", "From second address", "Python Weekly")
    _record(log, "m3", "c@y.com", "From other rule", "Other News")

    response = client.get("/log", params={"rule": "Python Weekly"})

    assert response.status_code == 200
    assert "From first address" in response.text
    assert "From second address" in response.text
    assert "From other rule" not in response.text
    assert '<option value="Python Weekly" selected' in response.text
    assert "Python Weekly &mdash; a@x.com, b@x.com" in response.text


def test_log_page_renders_rule_column_with_placeholder_for_unassigned_rows(tmp_path: Path) -> None:
    import sqlite3

    from em_phi.decision_log import DecisionLog

    client = _make_multi_rule_client(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    _record(log, "m1", "a@x.com", "Assigned row", "Python Weekly")
    conn = sqlite3.connect(tmp_path / "decisions.db")
    conn.execute("UPDATE decisions SET rule_name = NULL WHERE message_id = 'm1'")
    conn.commit()
    conn.close()
    _record(log, "m2", "c@y.com", "Named row", "Other News")

    html = client.get("/log").text

    assert '<th class="px-4 py-3 text-left">Rule</th>' in html
    assert ">Other News</td>" in html
    assert '<span class="text-gray-400">&mdash;</span>' in html


def test_startup_backfills_rule_names_and_survives_unwritable_db(tmp_path: Path) -> None:
    import sqlite3

    from em_phi.decision_log import DecisionLog

    client = _make_multi_rule_client(tmp_path)
    log = DecisionLog(tmp_path / "decisions.db")
    _record(log, "m1", "b@x.com", "Subject", "Python Weekly")
    conn = sqlite3.connect(tmp_path / "decisions.db")
    conn.execute("UPDATE decisions SET rule_name = NULL")
    conn.commit()
    conn.close()

    with client:  # runs the lifespan
        pass
    assert [e.rule_name for e in log.query()] == ["Python Weekly"]

    # A db path that can't be opened must only warn, not stop the server.
    broken = _make_multi_rule_client(tmp_path / "missing-dir")
    with broken:
        pass
