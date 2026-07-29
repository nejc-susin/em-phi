from pathlib import Path

from fastapi.testclient import TestClient

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, RuleConfig, WebConfig
from em_phi.web.app import create_app


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
    client.cookies.set("em_phi_auth", "secret")
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
