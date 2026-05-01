import os
from unittest.mock import MagicMock, patch

import pytest

from em_phi.classifiers.claude import ClaudeClassifier, _parse_verdict, _validate
from em_phi.config import AnthropicConfig, SenderConfig
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

@pytest.fixture
def classifier(monkeypatch: pytest.MonkeyPatch) -> ClaudeClassifier:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    return ClaudeClassifier(AnthropicConfig())


def test_classify_returns_verdict(classifier: ClaudeClassifier, relevant_email, sample_sender) -> None:
    response_text = '{"verdict": "relevant", "confidence": "high", "reason": "Python release."}'

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]

    with patch.object(classifier._client.messages, "create", return_value=mock_response):
        verdict = classifier.classify(relevant_email, sample_sender)

    assert verdict.verdict == "relevant"
    assert verdict.confidence == "high"


def test_classify_sends_interest_profile_in_system_prompt(
    classifier: ClaudeClassifier, relevant_email, sample_sender
) -> None:
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"verdict": "relevant", "confidence": "high", "reason": "R."}')]

    with patch.object(classifier._client.messages, "create", return_value=mock_response) as mock_create:
        classifier.classify(relevant_email, sample_sender)

    call_kwargs = mock_create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    system_text = system_blocks[0]["text"]

    assert sample_sender.interests.strip() in system_text
    assert sample_sender.tolerance in system_text
    assert "cache_control" in system_blocks[0]


def test_classify_includes_email_content_in_user_message(
    classifier: ClaudeClassifier, relevant_email, sample_sender
) -> None:
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"verdict": "irrelevant", "confidence": "low", "reason": "R."}')]

    with patch.object(classifier._client.messages, "create", return_value=mock_response) as mock_create:
        classifier.classify(relevant_email, sample_sender)

    messages = mock_create.call_args.kwargs["messages"]
    user_content = messages[0]["content"]
    assert relevant_email.subject in user_content
    assert relevant_email.body in user_content


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeClassifier(AnthropicConfig())
