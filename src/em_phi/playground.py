"""Profile playground: re-classify a rule's recent mail with an edited profile.

Read-only with respect to Gmail and the logs — nothing here labels, archives,
or writes the decision log or the run log.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from pydantic import ValidationError

from em_phi.classifiers.base import Classifier
from em_phi.classifiers.claude import build_prompt
from em_phi.config import RuleConfig
from em_phi.decision_log import DecisionLog, LogEntry
from em_phi.models import Email, Verdict
from em_phi.processor import _prepare_body
from em_phi.providers.base import EmailProvider

logger = logging.getLogger(__name__)


@dataclass
class PlaygroundItem:
    email: Email                # as fetched, raw body
    processed_email: Email      # body after _prepare_body — what the classifier sees
    system_prompt: str
    user_message: str
    logged: LogEntry | None = None
    verdict: Verdict | None = None
    error: str | None = None

    @property
    def changed(self) -> bool | None:
        """Whether the new verdict differs from the logged one; None if either is missing."""
        if self.logged is None or self.verdict is None:
            return None
        return self.verdict.verdict != self.logged.verdict


@dataclass
class PlaygroundResult:
    items: list[PlaygroundItem] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)


def trial_rule(rule: RuleConfig, interests: str, tolerance: str) -> RuleConfig:
    """A validated copy of rule with interests and tolerance replaced.

    Raises ValueError with a readable message on empty interests or an
    unknown tolerance.
    """
    try:
        return RuleConfig.model_validate({**rule.model_dump(), "interests": interests, "tolerance": tolerance})
    except ValidationError as e:
        messages = []
        for err in e.errors():
            msg = err["msg"].removeprefix("Value error, ")
            loc = ".".join(str(p) for p in err["loc"])
            messages.append(f"{loc}: {msg}" if loc else msg)
        raise ValueError("; ".join(messages)) from None


def run_playground(
    *,
    provider: EmailProvider,
    classifier: Classifier | None,
    log: DecisionLog | None,
    rule: RuleConfig,
    days: int,
    limit: int,
    max_workers: int = 4,
) -> PlaygroundResult:
    """Fetch the rule's recent mail and classify it with `rule` (prompts only if classifier is None).

    Raises if listing the messages fails; per-message fetch and classify
    errors are recorded instead.
    """
    result = PlaygroundResult()
    message_ids = provider.fetch_recent(rule.email, days, limit)

    # Sequential: the Gmail client is not thread-safe.
    for message_id in message_ids:
        try:
            email = provider.get_message(message_id)
        except Exception as exc:
            logger.warning("Playground: could not fetch %s: %s", message_id, exc)
            result.fetch_errors.append(f"{message_id}: {exc}")
            continue
        processed = replace(email, body=_prepare_body(email.body))
        system_prompt, user_message = build_prompt(processed, rule)
        result.items.append(PlaygroundItem(
            email=email,
            processed_email=processed,
            system_prompt=system_prompt,
            user_message=user_message,
            logged=log.get(message_id) if log is not None else None,
        ))

    if classifier is None or not result.items:
        return result

    def classify(item: PlaygroundItem) -> None:
        try:
            item.verdict = classifier.classify(item.processed_email, rule)
        except Exception as exc:
            logger.warning("Playground: classify failed for %s: %s", item.email.message_id, exc)
            item.error = str(exc)

    # The Anthropic client is thread-safe; each worker fills in its own item,
    # so result.items keeps the fetch order.
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        list(pool.map(classify, result.items))
    return result
