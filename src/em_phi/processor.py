from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from em_phi.actions import apply_verdict
from em_phi.classifiers.base import Classifier
from em_phi.config import AppConfig, SenderConfig
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider


@dataclass
class SenderResult:
    sender_email: str
    processed: int = 0
    relevant: int = 0
    irrelevant: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class RunSummary:
    results: list[SenderResult] = field(default_factory=list)

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
    sender_filter: str | None = None,
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> RunSummary:
    """Run the full processing loop over all configured senders.

    Args:
        config: Loaded application config.
        provider: Authenticated email provider.
        classifier: Classifier to determine relevance.
        log: Decision log for tracking and deduplication.
        dry_run: If True, classify but do not modify Gmail or write to the log.
        sender_filter: If set, process only the sender with this email address.
        on_email: Optional callback fired after each email is classified.
        on_error: Optional callback fired when a non-fatal error occurs.
    """
    summary = RunSummary()

    senders = config.senders
    if sender_filter:
        senders = [s for s in senders if s.email == sender_filter]

    for sender in senders:
        result = _process_sender(
            sender=sender,
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


def _process_sender(
    *,
    sender: SenderConfig,
    config: AppConfig,
    provider: EmailProvider,
    classifier: Classifier,
    log: DecisionLog,
    dry_run: bool,
    on_email: OnEmail | None,
    on_error: OnError | None,
) -> SenderResult:
    result = SenderResult(sender_email=sender.email)

    try:
        message_ids = provider.fetch_unread(sender.email)
    except Exception as exc:
        if on_error:
            on_error(f"fetch_unread({sender.email})", exc)
        result.errors += 1
        return result

    for msg_id in message_ids:
        if log.is_processed(msg_id):
            result.skipped += 1
            continue

        try:
            email = provider.get_message(msg_id)
            verdict = classifier.classify(email, sender)
            action = apply_verdict(
                email=email,
                verdict=verdict,
                sender=sender,
                labels=config.labels,
                provider=provider,
                dry_run=dry_run,
            )
        except Exception as exc:
            if on_error:
                on_error(f"processing message {msg_id}", exc)
            result.errors += 1
            continue

        if not dry_run:
            log.record(
                message_id=msg_id,
                sender=sender.email,
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

    return result
