from __future__ import annotations

import base64
import email.utils
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from em_phi.models import Email

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_MAX_RESULTS = 100


class GmailProvider:
    def __init__(
        self,
        credentials_file: Path,
        token_file: Path,
        fetch_label: str | None = None,
    ) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._fetch_label = fetch_label
        self._service: Any = None
        self._label_cache: dict[str, str] = {}  # label name → label id

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Load the saved token, refreshing it if expired."""
        if not self._token_file.exists():
            raise RuntimeError(
                f"Token not found: {self._token_file}\n"
                "See docs/gmail-setup.md for instructions on generating token.json."
            )

        creds = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                logger.info("Gmail: token expired, refreshing")
                creds.refresh(Request())
                self._token_file.write_text(creds.to_json())
                logger.debug("Gmail: token refreshed, saved to %s", self._token_file)
            else:
                raise RuntimeError(
                    f"Token is invalid and cannot be refreshed: {self._token_file}\n"
                    "Re-run the authorization script in docs/gmail-setup.md."
                )
        else:
            logger.debug("Gmail: credentials valid")

        self._service = build("gmail", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def fetch_unread(self, patterns: list[str]) -> list[str]:
        """Return message IDs of unread messages matching any of the sender patterns."""
        froms = [_pattern_to_from(p) for p in patterns]
        if len(froms) == 1:
            from_clause = froms[0]
        else:
            from_clause = "{" + " ".join(froms) + "}"
        query = f"{from_clause} is:unread"
        if self._fetch_label:
            query += f" label:{self._fetch_label}"

        logger.debug("Gmail: querying %r", query)
        try:
            result = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=_MAX_RESULTS)
                .execute()
            )
        except HttpError as e:
            logger.warning("Gmail API error listing messages: %s", e)
            raise RuntimeError(f"Gmail API error listing messages: {e}") from e

        ids = [m["id"] for m in result.get("messages", [])]
        logger.debug("Gmail: found %d message(s)", len(ids))
        return ids

    def get_message(self, message_id: str) -> Email:
        """Fetch and parse a full message."""
        logger.debug("Gmail: fetching message %s", message_id)
        try:
            raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as e:
            logger.warning("Gmail API error fetching %s: %s", message_id, e)
            raise RuntimeError(f"Gmail API error fetching message {message_id}: {e}") from e

        headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "")
        date_str = headers.get("Date", "")

        try:
            received_at = email.utils.parsedate_to_datetime(date_str)
        except Exception:
            logger.debug("Gmail: could not parse date %r for %s, using now()", date_str, message_id)
            received_at = datetime.now(tz=timezone.utc)

        body = _extract_body(raw["payload"])

        return Email(
            message_id=message_id,
            sender=sender,
            subject=subject,
            body=body,
            received_at=received_at,
        )

    def apply_label(self, message_id: str, label_name: str) -> None:
        """Apply label_name to the message, creating the label first if needed."""
        logger.debug("Gmail: applying label %r to %s", label_name, message_id)
        label_id = self._ensure_label(label_name)
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as e:
            logger.warning("Gmail API error applying label to %s: %s", message_id, e)
            raise RuntimeError(f"Gmail API error applying label to {message_id}: {e}") from e

    def archive(self, message_id: str) -> None:
        """Remove the message from INBOX without deleting it."""
        logger.debug("Gmail: archiving %s", message_id)
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
        except HttpError as e:
            logger.warning("Gmail API error archiving %s: %s", message_id, e)
            raise RuntimeError(f"Gmail API error archiving {message_id}: {e}") from e

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def _ensure_label(self, name: str) -> str:
        """Return the label ID for name, creating the label if it doesn't exist."""
        if name in self._label_cache:
            return self._label_cache[name]

        try:
            result = self._service.users().labels().list(userId="me").execute()
        except HttpError as e:
            logger.warning("Gmail API error listing labels: %s", e)
            raise RuntimeError(f"Gmail API error listing labels: {e}") from e

        for label in result.get("labels", []):
            if label["name"] == name:
                self._label_cache[name] = label["id"]
                return label["id"]

        # Label not found — create it
        logger.info("Gmail: creating label %r", name)
        try:
            new_label = (
                self._service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
        except HttpError as e:
            logger.warning("Gmail API error creating label %r: %s", name, e)
            raise RuntimeError(f"Gmail API error creating label '{name}': {e}") from e

        self._label_cache[name] = new_label["id"]
        return new_label["id"]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pattern_to_from(pattern: str) -> str:
    """Convert a sender pattern to a Gmail from: query clause.

    - exact email  → from:user@example.com
    - domain only  → from:@example.com
    """
    if "@" not in pattern:
        return f"from:@{pattern}"
    return f"from:{pattern}"


def _extract_body(payload: dict) -> str:
    """Recursively extract the plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return _b64decode(data) if data else ""

    # For multipart, prefer text/plain over other parts
    parts = payload.get("parts", [])
    plain = next((p for p in parts if p.get("mimeType") == "text/plain"), None)
    if plain:
        return _extract_body(plain)

    # Recurse into nested multipart
    for part in parts:
        result = _extract_body(part)
        if result:
            return result

    return ""


def _b64decode(data: str) -> str:
    # Gmail uses URL-safe base64; pad to a multiple of 4
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
