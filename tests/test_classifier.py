import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from em_phi.classifiers.claude import ClaudeClassifier, _parse_verdict, _validate
from em_phi.config import LLMConfig, RuleConfig
from em_phi.models import Verdict


# ------------------------------------------------------------------
# _parse_verdict — parsing strategies
# ------------------------------------------------------------------

def test_parse_direct_json() -> None:
    raw = '{"verdict": "relevant", "confidence": "high", "reason": "Covers a release."}'
    v = _parse_verdict(raw)
    assert v.verdict == "relevant"
    assert v.confidence == "high"
    assert v.reason == "Covers a release."


def test_parse_markdown_fence() -> None:
    raw = '```json\n{"verdict": "irrelevant", "confidence": "medium", "reason": "Off-topic."}\n```'
    v = _parse_verdict(raw)
    assert v.verdict == "irrelevant"
    assert v.confidence == "medium"


def test_parse_json_embedded_in_prose() -> None:
    raw = 'Based on the profile: {"verdict": "relevant", "confidence": "low", "reason": "Loosely related."} done.'
    v = _parse_verdict(raw)
    assert v.verdict == "relevant"


def test_parse_raises_on_unparseable() -> None:
    with pytest.raises(ValueError, match="Could not parse"):
        _parse_verdict("I think it is relevant but I forgot to format this as JSON.")


# ------------------------------------------------------------------
# _validate — field validation
# ------------------------------------------------------------------

def test_validate_bad_verdict_raises() -> None:
    with pytest.raises(ValueError, match="verdict"):
        _validate({"verdict": "maybe", "confidence": "high", "reason": "Reason."})


def test_validate_bad_confidence_degrades_to_medium() -> None:
    v = _validate({"verdict": "relevant", "confidence": "absolutely_certain", "reason": "R."})
    assert v.confidence == "medium"


def test_validate_missing_reason_raises() -> None:
    with pytest.raises(ValueError, match="reason"):
        _validate({"verdict": "relevant", "confidence": "high", "reason": ""})


# ------------------------------------------------------------------
# ClaudeClassifier — full call with mocked SDK
# ------------------------------------------------------------------

def _response(*blocks, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


_THINKING = SimpleNamespace(type="thinking", thinking="")


@pytest.fixture
def classifier(monkeypatch: pytest.MonkeyPatch) -> ClaudeClassifier:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    return ClaudeClassifier(LLMConfig())


def test_classify_returns_verdict(classifier: ClaudeClassifier, relevant_email, sample_rule) -> None:
    response_text = '{"verdict": "relevant", "confidence": "high", "reason": "Python release."}'

    mock_response = _response(_text(response_text))

    with patch.object(classifier._client.messages, "create", return_value=mock_response):
        verdict = classifier.classify(relevant_email, sample_rule)

    assert verdict.verdict == "relevant"
    assert verdict.confidence == "high"


def test_classify_sends_interest_profile_in_system_prompt(
    classifier: ClaudeClassifier, relevant_email, sample_rule
) -> None:
    mock_response = _response(_text('{"verdict": "relevant", "confidence": "high", "reason": "R."}'))

    with patch.object(classifier._client.messages, "create", return_value=mock_response) as mock_create:
        classifier.classify(relevant_email, sample_rule)

    call_kwargs = mock_create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    system_text = system_blocks[0]["text"]

    assert sample_rule.interests.strip() in system_text
    assert sample_rule.tolerance in system_text
    assert "cache_control" in system_blocks[0]


def test_classify_includes_email_content_in_user_message(
    classifier: ClaudeClassifier, relevant_email, sample_rule
) -> None:
    mock_response = _response(_text('{"verdict": "irrelevant", "confidence": "low", "reason": "R."}'))

    with patch.object(classifier._client.messages, "create", return_value=mock_response) as mock_create:
        classifier.classify(relevant_email, sample_rule)

    messages = mock_create.call_args.kwargs["messages"]
    user_content = messages[0]["content"]
    assert relevant_email.subject in user_content
    assert relevant_email.body in user_content


_VERDICT_JSON = '{"verdict": "relevant", "confidence": "high", "reason": "R."}'


def test_classify_skips_thinking_block(classifier: ClaudeClassifier, relevant_email, sample_rule) -> None:
    mock_response = _response(_THINKING, _text(_VERDICT_JSON))
    with patch.object(classifier._client.messages, "create", return_value=mock_response):
        verdict = classifier.classify(relevant_email, sample_rule)
    assert verdict.verdict == "relevant"


def test_classify_refusal_raises(classifier: ClaudeClassifier, relevant_email, sample_rule) -> None:
    mock_response = _response(stop_reason="refusal")
    with patch.object(classifier._client.messages, "create", return_value=mock_response):
        with pytest.raises(ValueError, match="refused"):
            classifier.classify(relevant_email, sample_rule)


def test_classify_max_tokens_without_text_raises(
    classifier: ClaudeClassifier, relevant_email, sample_rule
) -> None:
    mock_response = _response(_THINKING, stop_reason="max_tokens")
    with patch.object(classifier._client.messages, "create", return_value=mock_response):
        with pytest.raises(ValueError, match="max_tokens"):
            classifier.classify(relevant_email, sample_rule)


def test_classify_requests_low_effort_without_thinking_param(
    classifier: ClaudeClassifier, relevant_email, sample_rule
) -> None:
    mock_response = _response(_text(_VERDICT_JSON))
    with patch.object(classifier._client.messages, "create", return_value=mock_response) as mock_create:
        classifier.classify(relevant_email, sample_rule)
    kwargs = mock_create.call_args.kwargs
    assert kwargs["output_config"] == {"effort": "low"}
    assert "thinking" not in kwargs


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeClassifier(LLMConfig())


def test_build_prompt_converts_offset_to_utc(relevant_email, sample_rule) -> None:
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from em_phi.classifiers.claude import build_prompt

    tz = timezone(timedelta(hours=-7))
    email = replace(relevant_email, received_at=datetime(2026, 5, 1, 9, 0, tzinfo=tz))
    _, user = build_prompt(email, sample_rule)
    assert "2026-05-01 16:00 UTC" in user


def test_build_prompt_naive_datetime_treated_as_utc(relevant_email, sample_rule) -> None:
    from dataclasses import replace
    from datetime import datetime

    from em_phi.classifiers.claude import build_prompt

    email = replace(relevant_email, received_at=datetime(2026, 5, 1, 9, 0))
    _, user = build_prompt(email, sample_rule)
    assert "2026-05-01 09:00 UTC" in user


def test_parse_non_object_json_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_verdict('["relevant"]')
    with pytest.raises(ValueError):
        _validate(["relevant"])  # type: ignore[arg-type]


def test_parse_falls_through_after_non_object_json() -> None:
    v = _parse_verdict('"x" {"verdict": "relevant", "confidence": "high", "reason": "r"}')
    assert v.verdict == "relevant"
