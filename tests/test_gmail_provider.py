import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from em_phi.providers.gmail import GmailProvider, build_query, message_url, token_status


def _provider(fetch_label=None, **kwargs) -> GmailProvider:
    p = GmailProvider(Path("t.json"), fetch_label=fetch_label, **kwargs)
    p._service = MagicMock()
    return p


def _set_message(p: GmailProvider, date: str, label_ids: list[str] | None = None) -> None:
    data = base64.urlsafe_b64encode(b"hello").decode()
    p._service.users().messages().get().execute.return_value = {
        **({"labelIds": label_ids} if label_ids is not None else {}),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "S"},
                {"name": "From", "value": "a@example.com"},
                {"name": "Date", "value": date},
            ],
            "body": {"data": data},
        }
    }


def test_get_message_normalises_date_to_utc() -> None:
    p = _provider()
    _set_message(p, "Fri, 01 May 2026 09:00:00 -0700")
    assert p.get_message("m1").received_at == datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)


def test_get_message_naive_date_becomes_utc() -> None:
    p = _provider()
    _set_message(p, "Fri, 01 May 2026 09:00:00 -0000")
    got = p.get_message("m1").received_at
    assert got == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    assert got.tzinfo is not None


def _query(p: GmailProvider) -> str:
    return p._service.users().messages().list.call_args.kwargs["q"]


def test_fetch_label_with_spaces_is_quoted() -> None:
    p = _provider("To Filter")
    p._service.users().messages().list().execute.return_value = {}
    p.fetch_unread(["news@example.com"])
    assert _query(p).endswith('label:"To Filter"')


def test_fetch_label_without_spaces_unchanged() -> None:
    p = _provider("newsletters")
    p._service.users().messages().list().execute.return_value = {}
    p.fetch_unread(["news@example.com"])
    assert _query(p).endswith("label:newsletters")


def test_fetch_unread_by_default_queries_is_unread() -> None:
    p = _provider()
    p._service.users().messages().list().execute.return_value = {}
    p.fetch_unread(["news@example.com"])
    assert _query(p) == "from:news@example.com is:unread"


def test_fetch_includes_read_mail_when_configured() -> None:
    p = _provider("To Filter", unread_only=False, lookback_days=7)
    p._service.users().messages().list().execute.return_value = {"messages": [{"id": "a"}]}
    assert p.fetch_unread(["news@example.com"]) == ["a"]
    assert _query(p) == 'from:news@example.com newer_than:7d label:"To Filter"'


# ------------------------------------------------------------------ build_query


@pytest.mark.parametrize(
    ("patterns", "fetch_label", "unread_only", "expected"),
    [
        (["a@x.com"], None, True, "from:a@x.com is:unread"),
        (["a@x.com"], None, False, "from:a@x.com newer_than:5d"),
        (["a@x.com", "y.com"], None, True, "{from:a@x.com from:@y.com} is:unread"),
        (["a@x.com", "y.com"], None, False, "{from:a@x.com from:@y.com} newer_than:5d"),
        (["a@x.com"], "news", True, "from:a@x.com is:unread label:news"),
        (["a@x.com"], "news", False, "from:a@x.com newer_than:5d label:news"),
        (["a@x.com"], "To Filter", True, 'from:a@x.com is:unread label:"To Filter"'),
        (["a@x.com"], "To Filter", False, 'from:a@x.com newer_than:5d label:"To Filter"'),
        (["a@x.com", "y.com"], "To Filter", False, '{from:a@x.com from:@y.com} newer_than:5d label:"To Filter"'),
    ],
)
def test_build_query(patterns, fetch_label, unread_only, expected) -> None:
    assert build_query(patterns, fetch_label=fetch_label, unread_only=unread_only, lookback_days=5) == expected


def test_build_query_ignores_lookback_when_unread_only() -> None:
    assert "newer_than" not in build_query(["a@x.com"], fetch_label=None, unread_only=True, lookback_days=9)


# ------------------------------------------------------------------ unread flag


def test_get_message_unread_from_label_ids() -> None:
    p = _provider()
    _set_message(p, "Fri, 01 May 2026 09:00:00 -0000", ["INBOX", "UNREAD"])
    assert p.get_message("m1").unread is True


def test_get_message_read_when_unread_label_absent() -> None:
    p = _provider()
    _set_message(p, "Fri, 01 May 2026 09:00:00 -0000", ["INBOX"])
    assert p.get_message("m1").unread is False


def test_get_message_without_label_ids_is_read() -> None:
    p = _provider()
    _set_message(p, "Fri, 01 May 2026 09:00:00 -0000")
    assert p.get_message("m1").unread is False


# ------------------------------------------------------------------ message_url


def test_message_url_defaults_to_account_zero() -> None:
    assert message_url("abc123") == "https://mail.google.com/mail/u/0/#all/abc123"


def test_message_url_with_account_index() -> None:
    assert message_url("abc123", "2") == "https://mail.google.com/mail/u/2/#all/abc123"


def test_message_url_with_email_address() -> None:
    assert message_url("abc123", "me@example.com") == "https://mail.google.com/mail/u/me@example.com/#all/abc123"


def test_message_url_escapes_unsafe_account_characters() -> None:
    assert "/mail/u/a%2Fb/" in message_url("abc123", "a/b")


# ------------------------------------------------------------------ token_status

_HINT = "Run oauth/authorize.py (see docs/gmail-setup.md)"


def _token_json(**overrides) -> str:
    data = {
        "token": "expired-access-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "expiry": "2020-01-01T00:00:00Z",
    }
    data.update(overrides)
    return json.dumps({k: v for k, v in data.items() if v is not None})


def test_token_status_missing(tmp_path: Path) -> None:
    status = token_status(tmp_path / "token.json")
    assert (status.ok, status.summary, status.hint) == (False, "token.json not found", _HINT)


def test_token_status_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text("{not json")
    status = token_status(tmp_path / "token.json")
    assert (status.ok, status.summary, status.hint) == (False, "token.json is unreadable", _HINT)


def test_token_status_rejected_by_google_auth(tmp_path: Path) -> None:
    # Valid JSON with a refresh token, but missing the client fields Credentials needs.
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "r"}))
    status = token_status(tmp_path / "token.json")
    assert (status.ok, status.summary) == (False, "token.json is unreadable")


def test_token_status_no_refresh_token(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text(_token_json(refresh_token=None))
    status = token_status(tmp_path / "token.json")
    assert (status.ok, status.summary, status.hint) == (False, "token has no refresh token", _HINT)


def test_token_status_ok_even_with_an_expired_access_token(tmp_path: Path) -> None:
    (tmp_path / "token.json").write_text(_token_json())
    status = token_status(tmp_path / "token.json")
    assert (status.ok, status.summary, status.hint) == (True, "Connected", None)
