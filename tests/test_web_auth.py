from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, RuleConfig, WebConfig
from em_phi.web.app import _session_value, create_app

TOKEN = "secret"


def _make_client(tmp_path: Path, rule_name: str = "Example Newsletter", token: str = TOKEN) -> TestClient:
    config = AppConfig(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=Path("token.json"),
        ),
        decision_log=DecisionLogConfig(path=tmp_path / "decisions.db"),
        rules=[RuleConfig(email="newsletter@example.com", name=rule_name, interests="python")],
        web=WebConfig(auth_token=token),
    )
    return TestClient(create_app(config, tmp_path / "config.yaml"))


def _authed(tmp_path: Path, **kwargs) -> TestClient:
    client = _make_client(tmp_path, **kwargs)
    client.cookies.set("em_phi_auth", _session_value(TOKEN))
    return client


def test_login_sets_derived_cookie_not_raw_token(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/login/submit", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    value = response.cookies.get("em_phi_auth")
    assert value == _session_value(TOKEN)
    assert value != TOKEN


def test_login_wrong_token_is_401(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/login/submit", data={"token": "nope"}, follow_redirects=False)
    assert response.status_code == 401
    assert "em_phi_auth" not in response.cookies


def test_login_non_ascii_token_does_not_crash(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/login/submit", data={"token": "pässword"}, follow_redirects=False)
    assert response.status_code == 401


def test_raw_token_cookie_is_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.cookies.set("em_phi_auth", TOKEN)
    response = client.get("/log", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login"


def test_cross_site_post_is_blocked(tmp_path: Path) -> None:
    client = _authed(tmp_path)
    response = client.post(
        "/config/settings", data={}, headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_cross_site_login_submit_is_blocked(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post(
        "/login/submit", data={"token": TOKEN}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403


def test_same_origin_post_is_not_blocked(tmp_path: Path) -> None:
    client = _authed(tmp_path)
    response = client.post(
        "/config/settings", data={}, headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False
    )
    assert response.status_code != 403


def test_cross_site_run_stream_is_blocked_before_run_starts(tmp_path: Path) -> None:
    client = _authed(tmp_path)
    with patch("em_phi.web.routes.run._start_run", new=AsyncMock()) as start_run:
        response = client.get("/run/stream", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    start_run.assert_not_called()


def test_cross_site_plain_get_navigation_is_allowed(tmp_path: Path) -> None:
    client = _authed(tmp_path)
    response = client.get("/log", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


def test_delete_confirm_name_is_json_escaped(tmp_path: Path) -> None:
    client = _authed(tmp_path, rule_name='Bob\'s "news" <x>')
    html = client.get("/config").text
    assert "Delete rule Bob's" not in html
    assert "confirm(" in html
    assert "Bob\\u0027s" in html


def test_weak_token_banner_shown_on_authenticated_pages(tmp_path: Path) -> None:
    client = _authed(tmp_path)  # "secret" is shorter than 12 characters
    response = client.get("/log")
    assert "web.auth_token is the default or too short" in response.text


def test_default_token_banner_shown(tmp_path: Path) -> None:
    client = _make_client(tmp_path, token="change-me")
    client.cookies.set("em_phi_auth", _session_value("change-me"))
    assert "web.auth_token is the default or too short" in client.get("/log").text


def test_strong_token_has_no_banner(tmp_path: Path) -> None:
    strong = "a-long-random-token-1234"
    client = _make_client(tmp_path, token=strong)
    client.cookies.set("em_phi_auth", _session_value(strong))
    response = client.get("/log")
    assert response.status_code == 200
    assert "web.auth_token is the default or too short" not in response.text


def test_login_page_never_shows_banner(tmp_path: Path) -> None:
    client = _make_client(tmp_path)  # weak token
    response = client.get("/login")
    assert response.status_code == 200
    assert "too short" not in response.text
