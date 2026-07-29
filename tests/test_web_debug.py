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
        rules=[
            RuleConfig(email=["news1@example.com", "news2@example.com"], name="Multi News", interests="python"),
            RuleConfig(email="digest@example.com", name="Tech Digest", interests="systems"),
        ],
        web=WebConfig(auth_token="secret"),
    )
    app = create_app(config, tmp_path / "config.yaml")
    client = TestClient(app)
    client.cookies.set("em_phi_auth", "secret")
    return client


def test_debug_page_rule_dropdown_shows_address_and_rule_name(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.get("/debug")
    assert response.status_code == 200
    assert "news1@example.com &mdash; Multi News" in response.text
    assert "news2@example.com &mdash; Multi News" in response.text
    assert "digest@example.com &mdash; Tech Digest" in response.text


def test_debug_page_blank_limit_field_does_not_422(tmp_path: Path) -> None:
    """The Limit input can be cleared by the user; that must not surface
    FastAPI's raw int-coercion validation error."""
    client = _make_client(tmp_path)

    response = client.get("/debug", params={"limit": ""})
    assert response.status_code == 200


def test_debug_page_garbage_limit_is_ignored_not_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.get("/debug", params={"limit": "not-a-number"})
    assert response.status_code == 200
