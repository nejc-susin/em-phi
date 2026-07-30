from pathlib import Path

from fastapi.testclient import TestClient

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, RuleConfig, WebConfig
from em_phi.web.app import create_app

_EXTERNAL_HOSTS = ("cdn.tailwindcss.com", "unpkg.com")
_STATIC_ASSETS = ("/static/tailwind.js", "/static/htmx.min.js", "/static/htmx-sse.js")


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
    return TestClient(app)


def test_login_page_has_no_external_cdn_references(tmp_path: Path) -> None:
    """Pages must not depend on reaching an external CDN from the browser —
    on a network that can't resolve/reach it, a render-blocking <script> tag
    can hang the page load for a long time even though the server itself
    responds instantly."""
    client = _make_client(tmp_path)

    response = client.get("/login")
    assert response.status_code == 200
    for host in _EXTERNAL_HOSTS:
        assert host not in response.text


def test_authenticated_pages_have_no_external_cdn_references(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.cookies.set("em_phi_auth", "secret")

    response = client.get("/run")
    assert response.status_code == 200
    for host in _EXTERNAL_HOSTS:
        assert host not in response.text
    for asset in _STATIC_ASSETS:
        assert asset in response.text


def test_static_assets_are_served_without_authentication(tmp_path: Path) -> None:
    """The login page itself needs these before any auth cookie exists."""
    client = _make_client(tmp_path)

    for asset in _STATIC_ASSETS:
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} -> {response.status_code}"
        assert len(response.content) > 1000
