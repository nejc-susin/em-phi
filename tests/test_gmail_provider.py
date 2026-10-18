import base64
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from em_phi.providers.gmail import GmailProvider


def _provider(fetch_label=None) -> GmailProvider:
    p = GmailProvider(Path("t.json"), fetch_label=fetch_label)
    p._service = MagicMock()
    return p


def _set_message(p: GmailProvider, date: str) -> None:
    data = base64.urlsafe_b64encode(b"hello").decode()
    p._service.users().messages().get().execute.return_value = {
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
