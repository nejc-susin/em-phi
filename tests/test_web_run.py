import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
from fastapi.testclient import TestClient

from em_phi.config import AppConfig, DecisionLogConfig, EmailProviderConfig, RuleConfig, WebConfig
from em_phi.web.app import create_app
from em_phi.web.routes.run import _start_run, _start_stream, execute_run
from em_phi.web.state import AppState


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=Path("token.json"),
        ),
        decision_log=DecisionLogConfig(path=tmp_path / "decisions.db"),
        rules=[RuleConfig(email="newsletter@example.com", name="Example Newsletter", interests="python")],
        web=WebConfig(auth_token="secret"),
    )


def _make_client(tmp_path: Path) -> tuple[TestClient, AppState]:
    states: list[AppState] = []

    def capture(*args):
        states.append(AppState(*args))
        return states[-1]

    with patch("em_phi.web.app.AppState", side_effect=capture):
        app = create_app(_make_config(tmp_path), tmp_path / "config.yaml")
    client = TestClient(app)
    client.cookies.set("em_phi_auth", "secret")
    return client, states[0]


@contextmanager
def _fake_backends(emails, verdicts, gate: threading.Event | None = None):
    """Patch the provider/classifier factories _blocking_run uses."""
    provider = MagicMock()
    provider.fetch_unread.return_value = [e.message_id for e in emails]
    provider.get_message.side_effect = {e.message_id: e for e in emails}.__getitem__

    def classify(email, rule):
        if gate is not None:
            assert gate.wait(timeout=5), "test gate never released"
        return verdicts[email.message_id]

    classifier = MagicMock()
    classifier.classify.side_effect = classify

    with patch("em_phi.cli._build_provider", return_value=provider), \
         patch("em_phi.cli._build_classifier", return_value=classifier):
        yield provider, classifier


def _events(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


async def _drain(stream) -> list[dict]:
    return _events("".join([chunk async for chunk in stream]))


def test_stream_emits_verdicts_and_done_then_records_last_run(
    tmp_path, relevant_email, irrelevant_email, relevant_verdict, irrelevant_verdict,
) -> None:
    client, state = _make_client(tmp_path)
    verdicts = {"msg001": relevant_verdict, "msg002": irrelevant_verdict}

    with _fake_backends([relevant_email, irrelevant_email], verdicts):
        response = client.get("/run/stream", params={"dry_run": "true"})

    assert response.status_code == 200
    events = _events(response.text)
    assert [e["type"] for e in events] == ["verdict", "verdict", "done"]
    assert [e["subject"] for e in events[:2]] == [relevant_email.subject, irrelevant_email.subject]
    assert all(e["dry_run"] for e in events[:2])
    done = events[-1]
    assert (done["processed"], done["relevant"], done["irrelevant"], done["skipped"], done["errors"]) == (2, 1, 1, 0, 0)

    assert not state.is_running
    page = client.get("/run")
    assert "Last run:" in page.text
    assert "UTC" in page.text


def test_stream_reports_worker_failure_as_error_event(tmp_path) -> None:
    client, state = _make_client(tmp_path)

    with patch("em_phi.cli._build_provider", side_effect=click.ClickException("bad credentials")):
        response = client.get("/run/stream")

    assert _events(response.text) == [{"type": "error", "message": "bad credentials"}]
    assert not state.is_running
    assert state.last_run is None


def test_stream_while_running_emits_single_error_event(tmp_path) -> None:
    client, state = _make_client(tmp_path)
    asyncio.run(state.run_lock.acquire())
    try:
        page = client.get("/run")
        assert "Running…" in page.text

        response = client.get("/run/stream")
        assert response.status_code == 200
        assert _events(response.text) == [{"type": "error", "message": "A run is already in progress"}]
        assert state.run_lock.locked()
    finally:
        state.run_lock.release()


def test_execute_run_sets_last_run(
    tmp_path, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}):
        summary = asyncio.run(execute_run(state, dry_run=False, rule_filter=None))

    assert summary is not None and summary.processed == 1
    assert state.last_run is not None
    assert state.last_run.summary is summary
    assert state.last_run.dry_run is False
    assert state.last_run.finished_at.utcoffset().total_seconds() == 0
    assert not state.run_lock.locked()


def test_second_run_is_refused_while_one_is_in_progress(
    tmp_path, caplog, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")
    gate = threading.Event()

    async def scenario():
        first = await _start_run(state, dry_run=True, rule_filter=None)
        assert first is not None
        try:
            assert await _start_run(state, dry_run=True, rule_filter=None) is None
            with caplog.at_level(logging.INFO, logger="em_phi.web.routes.run"):
                assert await execute_run(state, dry_run=False, rule_filter=None) is None
            assert "skipped" in caplog.text

            stream = await _start_stream(state, dry_run=True, rule_filter=None)
            assert await _drain(stream) == [{"type": "error", "message": "A run is already in progress"}]
        finally:
            gate.set()
        await first

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}, gate):
        asyncio.run(scenario())

    assert not state.run_lock.locked()


def test_lock_held_until_worker_finishes_even_if_consumer_leaves(
    tmp_path, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")
    gate = threading.Event()

    async def scenario():
        stream = await _start_stream(state, dry_run=True, rule_filter=None)
        assert await stream.__anext__() == ": heartbeat\n\n"  # worker is blocked
        await stream.aclose()  # the browser went away

        assert state.run_lock.locked()
        assert state.is_running

        gate.set()
        await state.current_run
        assert not state.run_lock.locked()
        assert state.last_run is not None

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}, gate):
        asyncio.run(scenario())
