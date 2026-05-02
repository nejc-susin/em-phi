import logging

from em_phi.config import LabelsConfig, RuleConfig
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider

logger = logging.getLogger(__name__)


def apply_verdict(
    *,
    email: Email,
    verdict: Verdict,
    rule: RuleConfig,
    labels: LabelsConfig,
    provider: EmailProvider,
    dry_run: bool,
) -> str:
    """Apply the verdict to the email and return the action taken.

    Relevant emails are always labelled and kept in inbox.
    Irrelevant emails are labelled, and archived if rule.action == 'archive'.
    In dry_run mode no Gmail API calls are made.
    """
    if verdict.verdict == "relevant":
        label = labels.relevant
        action = "label"
    else:
        label = labels.irrelevant
        action = rule.action  # "label" or "archive"

    if dry_run:
        logger.debug("Actions: [DRY RUN] would label=%r action=%s on %s", label, action, email.message_id)
    else:
        provider.apply_label(email.message_id, label)
        logger.info("Actions: applied label=%r to %s", label, email.message_id)
        if action == "archive":
            provider.archive(email.message_id)
            logger.info("Actions: archived %s", email.message_id)

    return action
