from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic

from em_phi.config import LLMConfig, RuleConfig
from em_phi.models import Email, Verdict

logger = logging.getLogger(__name__)

_TOLERANCE_GUIDANCE = {
    "aggressive": "Archive anything not clearly relevant. When in doubt, archive.",
    "balanced": "Keep somewhat relevant emails. Archive only clearly irrelevant ones.",
    "conservative": "Keep anything even slightly relevant. Archive only obvious misses.",
}

# Adaptive thinking (on by default on current models) counts toward
# max_tokens, so a cap sized for the short JSON answer alone — e.g. the old
# default of 256 — can be used up by thinking before any text is produced.
MIN_MAX_TOKENS = 1024

_SYSTEM_TEMPLATE = """\
You are an email relevance classifier for a newsletter reader.

Determine whether an incoming email is relevant to the reader based on their \
interest profile and tolerance level. Respond only with a JSON object — no prose, \
no markdown fences, no explanation outside the JSON.

## Reader's interest profile for {rule_name}
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


def _as_utc(dt: datetime) -> datetime:
    """Naive datetimes are assumed to be UTC; aware ones are converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_prompt(email: Email, rule: RuleConfig) -> tuple[str, str]:
    """Return (system_prompt, user_message) for the given email and rule config."""
    system = _SYSTEM_TEMPLATE.format(
        rule_name=rule.name,
        interests=rule.interests.strip(),
        tolerance=rule.tolerance,
        tolerance_guidance=_TOLERANCE_GUIDANCE[rule.tolerance],
    )
    user = _USER_TEMPLATE.format(
        subject=email.subject,
        date=_as_utc(email.received_at).strftime("%Y-%m-%d %H:%M UTC"),
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
        if config.max_tokens < MIN_MAX_TOKENS:
            logger.info(
                "Claude: llm.max_tokens=%d is below %d; using %d so thinking leaves room for the answer",
                config.max_tokens, MIN_MAX_TOKENS, MIN_MAX_TOKENS,
            )
        self._max_tokens = max(config.max_tokens, MIN_MAX_TOKENS)

    def classify(self, email: Email, rule: RuleConfig) -> Verdict:
        system_prompt, user_message = build_prompt(email, rule)

        logger.debug("Claude: classifying %r (model=%s)", email.subject, self._model)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    # Cache the system prompt: same rule → same prompt → cache hit
                    # on subsequent emails from this rule within a run.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            # Classification needs little reasoning; low effort keeps the default
            # adaptive thinking short. No `thinking` param: "disabled" is rejected
            # by some models (e.g. Opus/Sonnet 5.5).
            output_config={"effort": "low"},
        )

        if response.stop_reason == "refusal":
            raise ValueError(f"Claude refused to classify {email.subject!r} (stop_reason=refusal)")

        # Skip thinking (and any other non-text) blocks.
        raw = "".join(b.text for b in response.content if b.type == "text")
        if not raw.strip():
            hint = (
                " The response hit max_tokens before producing text; "
                "raise llm.max_tokens in the config."
                if response.stop_reason == "max_tokens"
                else ""
            )
            raise ValueError(
                f"Claude returned no text for {email.subject!r} "
                f"(stop_reason={response.stop_reason}).{hint}"
            )
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
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
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
