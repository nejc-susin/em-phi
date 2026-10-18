import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from fastapi.testclient import TestClient

from em_phi.config import (
    AppConfig,
    DecisionLogConfig,
    EmailProviderConfig,
    RuleConfig,
    ScheduleConfig,
    WebConfig,
)
from em_phi.processor import RunSummary
from em_phi.run_log import RunLog
from em_phi.web.app import _session_value, create_app
from em_phi.web.routes.run import _start_run, _start_stream, execute_run
from em_phi.web.state import AppState


def _make_config(tmp_path: Path, **overrides) -> AppConfig:
    fields = dict(
        email_provider=EmailProviderConfig(
            credentials_file=Path("credentials.json"),
            token_file=tmp_path / "token.json",
        ),
        decision_log=DecisionLogConfig(path=tmp_path / "decisions.db"),
        rules=[RuleConfig(email="newsletter@example.com", name="Example Newsletter", interests="python")],
        web=WebConfig(auth_token="secret"),
    )
    fields.update(overrides)
    return AppConfig(**fields)


def _make_client(tmp_path: Path, **overrides) -> tuple[TestClient, AppState]:
    states: list[AppState] = []

    def capture(*args):
        states.append(AppState(*args))
        return states[-1]

    with patch("em_phi.web.app.AppState", side_effect=capture):
        app = create_app(_make_config(tmp_path, **overrides), tmp_path / "config.yaml")
    client = TestClient(app)
    client.cookies.set("em_phi_auth", _session_value("secret"))
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


def _runs(tmp_path: Path):
    return RunLog(tmp_path / "decisions.db").recent()


def test_stream_emits_verdicts_and_done_then_records_the_run(
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
    [run] = _runs(tmp_path)
    assert (run.trigger, run.dry_run, run.rule_filter, run.status) == ("manual", True, None, "ok")
    assert (run.processed, run.relevant, run.irrelevant, run.skipped, run.errors) == (2, 1, 1, 0, 0)
    assert run.finished_at is not None

    page = client.get("/run")
    assert "Recent runs" in page.text
    assert run.started_at[:16] in page.text


def test_stream_reports_worker_failure_as_error_event(tmp_path) -> None:
    client, state = _make_client(tmp_path)

    with patch("em_phi.cli._build_provider", side_effect=click.ClickException("bad credentials")):
        response = client.get("/run/stream")

    assert _events(response.text) == [{"type": "error", "message": "bad credentials"}]
    assert not state.is_running
    [run] = _runs(tmp_path)
    assert (run.trigger, run.status, run.failure) == ("manual", "failed", "bad credentials")


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


def test_execute_run_records_a_scheduled_run(
    tmp_path, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}):
        summary = asyncio.run(execute_run(state, dry_run=False, rule_filter=None))

    assert summary is not None and summary.processed == 1
    [run] = _runs(tmp_path)
    assert (run.trigger, run.dry_run, run.status, run.processed, run.relevant) == ("scheduled", False, "ok", 1, 1)
    assert not state.run_lock.locked()


def test_scheduled_run_errors_are_recorded(
    tmp_path, relevant_email, irrelevant_email, relevant_verdict,
) -> None:
    """Scheduled runs have no SSE consumer; their per-email errors must still be kept."""
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")

    with _fake_backends([relevant_email, irrelevant_email], {"msg001": relevant_verdict}) as (provider, _):
        def get_message(message_id):
            if message_id == "msg002":
                raise RuntimeError("Gmail API error fetching message msg002")
            return relevant_email

        provider.get_message.side_effect = get_message
        summary = asyncio.run(execute_run(state, dry_run=False, rule_filter=None))

    assert summary is not None and summary.errors == 1
    [run] = _runs(tmp_path)
    assert (run.trigger, run.status, run.processed, run.errors) == ("scheduled", "errors", 1, 1)
    assert run.error_messages == ["processing message msg002: Gmail API error fetching message msg002"]


def test_run_whose_authentication_fails_is_recorded_as_failed(tmp_path) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")

    with _fake_backends([], {}) as (provider, _):
        provider.authenticate.side_effect = RuntimeError("invalid_grant: Token has been expired or revoked.")
        with pytest.raises(RuntimeError, match="invalid_grant"):
            asyncio.run(execute_run(state, dry_run=True, rule_filter=None))

    [run] = _runs(tmp_path)
    assert (run.trigger, run.dry_run, run.status) == ("scheduled", True, "failed")
    assert run.failure == "invalid_grant: Token has been expired or revoked."
    assert run.finished_at is not None
    assert not state.run_lock.locked()


def test_run_log_write_failure_does_not_break_the_run(
    tmp_path, caplog, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}), \
         patch.object(RunLog, "start", side_effect=OSError("disk full")), \
         caplog.at_level(logging.WARNING, logger="em_phi.run_log"):
        summary = asyncio.run(execute_run(state, dry_run=False, rule_filter=None))

    assert summary is not None and summary.processed == 1
    assert "disk full" in caplog.text
    assert not state.run_lock.locked()


def test_second_run_is_refused_while_one_is_in_progress(
    tmp_path, caplog, relevant_email, relevant_verdict,
) -> None:
    state = AppState(_make_config(tmp_path), tmp_path / "config.yaml")
    gate = threading.Event()

    async def scenario():
        first = await _start_run(state, dry_run=True, rule_filter=None, trigger="manual")
        assert first is not None
        try:
            assert await _start_run(state, dry_run=True, rule_filter=None, trigger="manual") is None
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
        assert _runs(tmp_path)[0].status == "ok"

    with _fake_backends([relevant_email], {"msg001": relevant_verdict}, gate):
        asyncio.run(scenario())


# ------------------------------------------------------------------ status panel


def _write_token(path: Path, **fields) -> None:
    data = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
    }
    data.update(fields)
    path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))


def test_run_page_shows_status_cards_and_recent_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    _write_token(tmp_path / "token.json")
    run_log = RunLog(tmp_path / "decisions.db")
    ok_id = run_log.start(trigger="cli", dry_run=False, rule_filter=None)
    run_log.finish(ok_id, RunSummary(), [])
    bad_id = run_log.start(trigger="scheduled", dry_run=True, rule_filter="newsletter@example.com")
    run_log.finish(bad_id, _summary(processed=3, errors=1), ["processing message m9: boom"])
    client, _ = _make_client(tmp_path)

    page = client.get("/run").text

    for title in ("Gmail", "Claude", "Schedule", "Last run"):
        assert f">{title}<" in page
    assert "Connected" in page
    assert "API key set" in page
    assert "sk-ant" not in page
    assert "Disabled" in page
    assert "Last successful access: " in page and "never" not in page
    # Last run card + Recent runs table.
    assert "Recent runs" in page
    assert 'id="recent-runs"' in page
    assert "scheduled" in page and "cli" in page
    assert "newsletter@example.com" in page
    assert "<details" in page
    assert "processing message m9: boom" in page


def test_run_page_without_a_database_shows_no_runs_and_does_not_create_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, _ = _make_client(tmp_path)

    page = client.get("/run").text

    assert "No runs yet" in page
    assert "token.json not found" in page
    assert "Run oauth/authorize.py (see docs/gmail-setup.md)" in page
    assert "ANTHROPIC_API_KEY not set" in page
    assert "Last successful access: never" in page
    assert not (tmp_path / "decisions.db").exists()


@pytest.mark.parametrize("content, expected", [
    ("{not json", "token.json is unreadable"),
    (json.dumps({"token": "t", "client_id": "c", "client_secret": "s"}), "token has no refresh token"),
])
def test_run_page_reports_a_bad_token(tmp_path, content, expected) -> None:
    (tmp_path / "token.json").write_text(content)
    client, _ = _make_client(tmp_path)

    page = client.get("/run").text

    assert expected in page
    assert "Connected" not in page


def test_run_page_flags_a_rejected_refresh_token(tmp_path) -> None:
    _write_token(tmp_path / "token.json")
    run_log = RunLog(tmp_path / "decisions.db")
    run_log.fail(
        run_log.start(trigger="scheduled", dry_run=False, rule_filter=None),
        "('invalid_grant: Token has been expired or revoked.', ...)", [],
    )
    client, _ = _make_client(tmp_path)

    page = client.get("/run").text

    assert "Google rejected the refresh token — re-run oauth/authorize.py" in page
    assert "Connected" not in page


def test_run_page_shows_the_next_scheduled_run(tmp_path) -> None:
    client, _ = _make_client(tmp_path, schedule=ScheduleConfig(enabled=True, interval_hours=6))

    with client:  # runs the lifespan, which starts the scheduler
        page = client.get("/run").text

    assert "Every 6h" in page
    assert "Next run: " in page


def test_startup_marks_stale_running_runs_as_interrupted(tmp_path) -> None:
    import sqlite3

    run_log = RunLog(tmp_path / "decisions.db")
    run_id = run_log.start(trigger="manual", dry_run=False, rule_filter=None)
    with sqlite3.connect(tmp_path / "decisions.db") as conn:
        conn.execute("UPDATE runs SET started_at = '2020-01-01 00:00:00' WHERE id = ?", (run_id,))
    client, _ = _make_client(tmp_path)

    with client:
        pass

    run = run_log.last()
    assert run is not None and run.status == "failed"
    assert run.failure is not None and "Interrupted" in run.failure


def _summary(**counts) -> RunSummary:
    from em_phi.processor import RuleResult

    return RunSummary(results=[RuleResult(rule_email="newsletter@example.com", **counts)])
