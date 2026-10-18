import logging

from em_phi.config import LabelsConfig, RuleConfig
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider

logger = logging.getLogger(__name__)

# Values apply_verdict returns, and therefore what the decision log stores in action_taken.
ACTIONS_TAKEN: tuple[str, ...] = ("label", "archive", "inbox")


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

    action: label  — label only (relevant stays in inbox, irrelevant stays wherever it is)
    action: archive — label + archive irrelevant (relevant stays in inbox)
    action: inbox  — label + move relevant to inbox, label + archive irrelevant
                     (already-read relevant mail is only labelled, not moved)

    In dry_run mode no Gmail API calls are made.
    """
    if verdict.verdict == "relevant":
        label = labels.relevant
        action = "inbox" if rule.action == "inbox" else "label"
    else:
        label = labels.irrelevant
        action = "archive" if rule.action in ("archive", "inbox") else "label"

    # Don't pull a message the user has already read back into the inbox.
    if action == "inbox" and not email.unread:
        action = "label"

    if dry_run:
        logger.debug("Actions: [DRY RUN] would label=%r action=%s on %s", label, action, email.message_id)
    else:
        provider.apply_label(email.message_id, label)
        logger.info("Actions: applied label=%r to %s", label, email.message_id)
        if action == "archive":
            provider.archive(email.message_id)
            logger.info("Actions: archived %s", email.message_id)
        elif action == "inbox":
            provider.move_to_inbox(email.message_id)
            logger.info("Actions: moved %s to inbox", email.message_id)

    return action
