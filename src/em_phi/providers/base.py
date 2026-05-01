from typing import Protocol, runtime_checkable

from em_phi.models import Email


@runtime_checkable
class EmailProvider(Protocol):
    """Protocol for email provider implementations.

    Implement this to add support for providers other than Gmail
    (e.g. IMAP, Outlook). The processor depends only on this interface.
    """

    def authenticate(self) -> None:
        """Load credentials and verify the connection is ready."""
        ...

    def fetch_unread(self, patterns: list[str]) -> list[str]:
        """Return message IDs of unread messages matching the given sender patterns.

        Each pattern is either an exact email address or a bare domain name.
        Multiple patterns are OR-combined.
        """
        ...

    def get_message(self, message_id: str) -> Email:
        """Fetch and return a fully-populated Email for the given message ID."""
        ...

    def apply_label(self, message_id: str, label_name: str) -> None:
        """Apply a label to the message, creating it if it doesn't exist."""
        ...

    def archive(self, message_id: str) -> None:
        """Remove the message from the inbox without deleting it."""
        ...
