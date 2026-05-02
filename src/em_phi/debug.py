from __future__ import annotations

from dataclasses import dataclass, replace

from em_phi.classifiers.claude import build_prompt
from em_phi.config import AppConfig, SenderConfig
from em_phi.models import Email
from em_phi.processor import _prepare_body
from em_phi.providers.base import EmailProvider


@dataclass
class DebugInfo:
    email: Email
    processed_email: Email
    sender: SenderConfig
    system_prompt: str
    user_message: str


def fetch_debug_info(
    config: AppConfig,
    provider: EmailProvider,
    sender_filter: str | None = None,
    limit: int = 1,
) -> list[DebugInfo]:
    """Fetch unread emails and build classifier prompts without calling the LLM.

    Only works with the built-in 'claude' classifier.
    """
    senders = (
        [s for s in config.senders if sender_filter in s.email]
        if sender_filter
        else config.senders
    )

    results: list[DebugInfo] = []

    for s in senders:
        if len(results) >= limit:
            break

        message_ids = provider.fetch_unread(s.email)

        for msg_id in message_ids:
            if len(results) >= limit:
                break

            email = provider.get_message(msg_id)
            processed = replace(email, body=_prepare_body(email.body))
            system_prompt, user_message = build_prompt(processed, s)

            results.append(DebugInfo(
                email=email,
                processed_email=processed,
                sender=s,
                system_prompt=system_prompt,
                user_message=user_message,
            ))

    return results
