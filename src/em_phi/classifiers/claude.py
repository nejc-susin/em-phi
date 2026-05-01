from __future__ import annotations

import json
import logging
import os
import re

import anthropic

from em_phi.config import LLMConfig, SenderConfig
from em_phi.models import Email, Verdict

logger = logging.getLogger(__name__)

_TOLERANCE_GUIDANCE = {
    "aggressive": "Archive anything not clearly relevant. When in doubt, archive.",
    "balanced": "Keep somewhat relevant emails. Archive only clearly irrelevant ones.",
    "conservative": "Keep anything even slightly relevant. Archive only obvious misses.",
}

_SYSTEM_TEMPLATE = """\
You are an email relevance classifier for a newsletter reader.

Determine whether an incoming email is relevant to the reader based on their \
interest profile and tolerance level. Respond only with a JSON object — no prose, \
no markdown fences, no explanation outside the JSON.

## Reader's interest profile for {sender_name}
{interests}

## Tolerance: {tolerance}
{tolerance_guidance}

## Response format
{{"verdict": "relevant" or "irrelevant", "confidence": "high" or "medium" or "low", "reason": "one sentence"}}"""

_USER_TEMPLATE = """\
## Email to classify
Subject: {subject}
Date: {date}

{body}"""


def build_prompt(email: Email, sender: SenderConfig) -> tuple[str, str]:
    """Return (system_prompt, user_message) for the given email and sender config."""
    system = _SYSTEM_TEMPLATE.format(
        sender_name=sender.name,
        interests=sender.interests.strip(),
        tolerance=sender.tolerance,
        tolerance_guidance=_TOLERANCE_GUIDANCE[sender.tolerance],
    )
    user = _USER_TEMPLATE.format(
        subject=email.subject,
        date=email.received_at.strftime("%Y-%m-%d %H:%M UTC"),
        body=email.body,
    )
    return system, user


class ClaudeClassifier:
    def __init__(self, config: LLMConfig) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is not set.\n"
                "Export it before running em-phi: export ANTHROPIC_API_KEY=sk-ant-..."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = config.model
        self._max_tokens = config.max_tokens

    def classify(self, email: Email, sender: SenderConfig) -> Verdict:
        system_prompt, user_message = build_prompt(email, sender)

        logger.debug("Claude: classifying %r (model=%s)", email.subject, self._model)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    # Cache the system prompt: same sender → same prompt → cache hit
                    # on subsequent emails from this sender within a run.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text
        logger.debug("Claude: raw response: %r", raw)
        return _parse_verdict(raw)


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------

def _parse_verdict(text: str) -> Verdict:
    """Parse a Verdict from Claude's response text, with fallback strategies."""
    # 1. Direct JSON parse
    try:
        return _validate(json.loads(text.strip()))
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. JSON inside a markdown code block
    logger.debug("Claude: direct JSON failed, trying markdown fence")
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return _validate(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. First JSON object found anywhere in the response
    logger.debug("Claude: markdown fence failed, trying regex")
    match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if match:
        try:
            return _validate(json.loads(match.group(0)))
        except (json.JSONDecodeError, ValueError):
            pass

    logger.error("Claude: all parse strategies failed: %r", text)
    raise ValueError(f"Could not parse verdict from Claude response:\n{text!r}")


def _validate(data: dict) -> Verdict:
    verdict = str(data.get("verdict", "")).lower()
    confidence = str(data.get("confidence", "")).lower()
    reason = str(data.get("reason", "")).strip()

    if verdict not in ("relevant", "irrelevant"):
        raise ValueError(f"Unexpected verdict value: {verdict!r}")
    if confidence not in ("high", "medium", "low"):
        logger.warning("Claude: unexpected confidence %r, normalizing to 'medium'", confidence)
        confidence = "medium"
    if not reason:
        raise ValueError("Missing reason in verdict")

    return Verdict(verdict=verdict, confidence=confidence, reason=reason)  # type: ignore[arg-type]
