from em_phi.config import LabelsConfig, SenderConfig
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider


def apply_verdict(
    *,
    email: Email,
    verdict: Verdict,
    sender: SenderConfig,
    labels: LabelsConfig,
    provider: EmailProvider,
    dry_run: bool,
) -> str:
    """Apply the verdict to the email and return the action taken.

    Relevant emails are always labelled and kept in inbox.
    Irrelevant emails are labelled, and archived if sender.action == 'archive'.
    In dry_run mode no Gmail API calls are made.
    """
    if verdict.verdict == "relevant":
        label = labels.relevant
        action = "label"
    else:
        label = labels.irrelevant
        action = sender.action  # "label" or "archive"

    if not dry_run:
        provider.apply_label(email.message_id, label)
        if action == "archive":
            provider.archive(email.message_id)

    return action
