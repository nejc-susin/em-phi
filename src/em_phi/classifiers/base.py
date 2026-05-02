from typing import Protocol, runtime_checkable

from em_phi.config import RuleConfig
from em_phi.models import Email, Verdict


@runtime_checkable
class Classifier(Protocol):
    """Protocol for email classifier implementations.

    Implement this to swap Claude for a different model (local LLM, GPT, etc.).
    The processor depends only on this interface.
    """

    def classify(self, email: Email, rule: RuleConfig) -> Verdict:
        """Classify email as relevant or irrelevant given the rule's interest profile."""
        ...
