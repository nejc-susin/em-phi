from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class Email:
    message_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime


@dataclass
class Verdict:
    verdict: Literal["relevant", "irrelevant"]
    confidence: Literal["high", "medium", "low"]
    reason: str
