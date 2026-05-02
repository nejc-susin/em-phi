from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Callable

logger = logging.getLogger(__name__)

_BODY_LIMIT = 4000

from em_phi.actions import apply_verdict
from em_phi.classifiers.base import Classifier
from em_phi.config import AppConfig, RuleConfig
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider


@dataclass
class RuleResult:
    rule_email: str
    processed: int = 0
    relevant: int = 0
    irrelevant: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class RunSummary:
    results: list[RuleResult] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return sum(r.processed for r in self.results)

    @property
    def relevant(self) -> int:
        return sum(r.relevant for r in self.results)

    @property
    def irrelevant(self) -> int:
        return sum(r.irrelevant for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.results)

    @property
    def errors(self) -> int:
        return sum(r.errors for r in self.results)


# Callback type for per-email progress reporting
OnEmail = Callable[[Email, Verdict, str, bool], None]  # email, verdict, action, dry_run
OnError = Callable[[str, Exception], None]              # context, exception


def process_all(
    *,
    config: AppConfig,
    provider: EmailProvider,
    classifier: Classifier,
    log: DecisionLog,
    dry_run: bool,
    rule_filter: str | None = None,
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> RunSummary:
    """Run the full processing loop over all configured rules."""
    summary = RunSummary()

    rules = config.rules
    if rule_filter:
        rules = [r for r in rules if rule_filter in r.email]

    for rule in rules:
        result = _process_rule(
            rule=rule,
            config=config,
            provider=provider,
            classifier=classifier,
            log=log,
            dry_run=dry_run,
            on_email=on_email,
            on_error=on_error,
        )
        summary.results.append(result)

    return summary


def _process_rule(
    *,
    rule: RuleConfig,
    config: AppConfig,
    provider: EmailProvider,
    classifier: Classifier,
    log: DecisionLog,
    dry_run: bool,
    on_email: OnEmail | None,
    on_error: OnError | None,
) -> RuleResult:
    result = RuleResult(rule_email=rule.email[0])

    logger.debug("Processor: starting %s", rule.email[0])
    try:
        message_ids = provider.fetch_unread(rule.email)
    except Exception as exc:
        logger.error("Processor: fetch_unread failed for %s: %s", rule.email, exc)
        if on_error:
            on_error(f"fetch_unread({rule.email})", exc)
        result.errors += 1
        return result

    for msg_id in message_ids:
        if log.is_processed(msg_id):
            logger.debug("Processor: skipping %s (already processed)", msg_id)
            result.skipped += 1
            continue

        try:
            email = provider.get_message(msg_id)
            email = replace(email, body=_prepare_body(email.body))
            verdict = classifier.classify(email, rule)
            action = apply_verdict(
                email=email,
                verdict=verdict,
                rule=rule,
                labels=config.labels,
                provider=provider,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.error("Processor: error on message %s: %s", msg_id, exc)
            if on_error:
                on_error(f"processing message {msg_id}", exc)
            result.errors += 1
            continue

        if not dry_run:
            log.record(
                message_id=msg_id,
                sender=rule.email[0],
                subject=email.subject,
                received_at=email.received_at,
                verdict=verdict,
                action_taken=action,
            )

        result.processed += 1
        if verdict.verdict == "relevant":
            result.relevant += 1
        else:
            result.irrelevant += 1

        if on_email:
            on_email(email, verdict, action, dry_run)

    logger.info(
        "Processor: %s done — processed=%d skipped=%d errors=%d",
        rule.email[0], result.processed, result.skipped, result.errors,
    )
    return result


# ------------------------------------------------------------------
# Body preprocessing
# ------------------------------------------------------------------

_URL_RE = re.compile(r"<?(?:https?://|www\.)\S+>?")


def _prepare_body(body: str) -> str:
    """Strip links and truncate body before sending to the classifier."""
    stripped = _URL_RE.sub("<link>", body)
    collapsed = " ".join(stripped.split())
    return collapsed[:_BODY_LIMIT]
