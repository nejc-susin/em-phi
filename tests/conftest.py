import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, LabelsConfig, LLMConfig, SenderConfig
from em_phi.models import Email, Verdict


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "decisions.db"


@pytest.fixture
def sample_sender() -> SenderConfig:
    return SenderConfig(
        email="newsletter@example.com",
        name="Example Newsletter",
        interests="I care about Python releases and security updates.",
        tolerance="balanced",
        action="label",
    )


@pytest.fixture
def sample_sender_archive() -> SenderConfig:
    return SenderConfig(
        email="digest@example.com",
        name="Tech Digest",
        interests="Distributed systems and database internals.",
        tolerance="aggressive",
        action="archive",
    )


@pytest.fixture
def sample_config(tmp_db: Path, sample_sender: SenderConfig) -> AppConfig:
    return AppConfig(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=Path("token.json"),
        ),
        decision_log=DecisionLogConfig(path=tmp_db),
        senders=[sample_sender],
    )


@pytest.fixture
def relevant_email() -> Email:
    return Email(
        message_id="msg001",
        sender="newsletter@example.com",
        subject="Python 3.14 released",
        body="Python 3.14 is now available with free-threaded mode stable.",
        received_at=datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def irrelevant_email() -> Email:
    return Email(
        message_id="msg002",
        sender="newsletter@example.com",
        subject="Join our community meetup",
        body="We're hosting a meetup next Saturday. All are welcome!",
        received_at=datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def relevant_verdict() -> Verdict:
    return Verdict(verdict="relevant", confidence="high", reason="Covers a Python release.")


@pytest.fixture
def irrelevant_verdict() -> Verdict:
    return Verdict(verdict="irrelevant", confidence="high", reason="Community event, not technical.")


@pytest.fixture
def sample_gmail_payloads() -> list[dict]:
    fixtures_path = Path(__file__).parent / "fixtures" / "sample_emails.json"
    return json.loads(fixtures_path.read_text())
