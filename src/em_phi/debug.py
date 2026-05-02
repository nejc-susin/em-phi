from __future__ import annotations

from dataclasses import dataclass, replace

from em_phi.classifiers.claude import build_prompt
from em_phi.config import AppConfig, RuleConfig
from em_phi.models import Email
from em_phi.processor import _prepare_body
from em_phi.providers.base import EmailProvider


@dataclass
class DebugInfo:
    email: Email
    processed_email: Email
    rule: RuleConfig
    system_prompt: str
    user_message: str


def fetch_debug_info(
    config: AppConfig,
    provider: EmailProvider,
    rule_filter: str | None = None,
    limit: int = 1,
) -> list[DebugInfo]:
    """Fetch unread emails and build classifier prompts without calling the LLM.

    Only works with the built-in 'claude' classifier.
    """
    rules = (
        [r for r in config.rules if rule_filter in r.email]
        if rule_filter
        else config.rules
    )

    results: list[DebugInfo] = []

    for rule in rules:
        if len(results) >= limit:
            break

        message_ids = provider.fetch_unread(rule.email)

        for msg_id in message_ids:
            if len(results) >= limit:
                break

            email = provider.get_message(msg_id)
            processed = replace(email, body=_prepare_body(email.body))
            system_prompt, user_message = build_prompt(processed, rule)

            results.append(DebugInfo(
                email=email,
                processed_email=processed,
                rule=rule,
                system_prompt=system_prompt,
                user_message=user_message,
            ))

    return results
