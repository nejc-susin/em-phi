from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.processor import OnEmail, OnError, RunSummary, process_all
from em_phi.run_log import RunLog, RunRecord, RunRecorder
from em_phi.web.routes._helpers import message_link
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState

logger = logging.getLogger(__name__)

# Queue sentinel: the run task has finished and its final event is queued.
_END = None

_RECENT_RUNS = 10
_INVALID_GRANT_SUMMARY = "Google rejected the refresh token — re-run oauth/authorize.py"


def router(state: AppState, templates: Jinja2Templates, scheduler: EmPhiScheduler) -> APIRouter:
    r = APIRouter()

    @r.get("/run", response_class=HTMLResponse)
    async def run_page(request: Request):
        return templates.TemplateResponse(request, "run.html", {
            "rules": state.config.rules,
            "is_running": state.is_running,
            "status": build_status(state, scheduler),
        })

    @r.get("/run/stream")
    async def stream_run(dry_run: bool = False, rule: str | None = None):
        # The run (and its lock) is started here, before the response starts
        # streaming, so it never depends on the client staying connected.
        return StreamingResponse(
            await _start_stream(state, dry_run=dry_run, rule_filter=rule),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return r


async def execute_run(
    state: AppState,
    dry_run: bool,
    rule_filter: str | None,
) -> RunSummary | None:
    """Run process_all in a thread pool; used by the scheduler.

    Returns None without running if another run is already in progress.
    """
    task = await _start_run(state, dry_run, rule_filter, "scheduled")
    if task is None:
        logger.info("Scheduled run skipped: a run is already in progress")
        return None
    # shield: cancelling the caller must not cancel the run (and release its
    # lock) while the worker thread is still processing emails.
    return await asyncio.shield(task)


async def _start_run(
    state: AppState,
    dry_run: bool,
    rule_filter: str | None,
    trigger: str,
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> asyncio.Task[RunSummary] | None:
    """Acquire the run lock and start a run in a worker thread.

    Returns None if a run is already in progress. Otherwise returns the run
    task, which holds the lock until the worker thread has finished. The
    worker records the run in the run log (see _blocking_run).
    """
    if state.run_lock.locked():
        return None
    # An uncontended asyncio.Lock.acquire() completes without suspending, so
    # there is no interleaving point between the check above and this line.
    await state.run_lock.acquire()

    loop = asyncio.get_running_loop()

    async def run() -> RunSummary:
        try:
            return await loop.run_in_executor(
                None, _blocking_run, state, dry_run, rule_filter, trigger, on_email, on_error
            )
        except Exception as exc:
            logger.error("Run failed: %s", exc)
            raise
        finally:
            state.run_lock.release()

    task = loop.create_task(run())
    state.current_run = task
    return task


async def _start_stream(
    state: AppState, dry_run: bool, rule_filter: str | None
) -> AsyncIterator[str]:
    """Start a run and return the SSE event stream for it.

    If a run is already in progress, the stream is a single error event.
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Called from the worker thread. call_soon_threadsafe callbacks run in
    # FIFO order, and the executor future is resolved the same way after the
    # worker returns, so every event is queued before the run task finishes.
    def emit(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def on_email(email: Email, verdict: Verdict, action: str, is_dry: bool) -> None:
        emit({
            "type": "verdict",
            "subject": email.subject,
            "url": message_link(state.config, email.message_id),
            "sender": email.sender,
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "action": action,
            "dry_run": is_dry,
        })

    def on_error(context: str, exc: Exception) -> None:
        emit({"type": "error", "context": context, "message": str(exc)})

    task = await _start_run(state, dry_run, rule_filter, "manual", on_email, on_error)
    if task is None:
        return _single_event({"type": "error", "message": "A run is already in progress"})

    def on_finished(t: asyncio.Task[RunSummary]) -> None:
        if t.cancelled():
            queue.put_nowait({"type": "error", "message": "Run cancelled"})
        elif (exc := t.exception()) is not None:
            queue.put_nowait({"type": "error", "message": str(exc)})
        else:
            summary = t.result()
            queue.put_nowait({
                "type": "done",
                "processed": summary.processed,
                "relevant": summary.relevant,
                "irrelevant": summary.irrelevant,
                "skipped": summary.skipped,
                "errors": summary.errors,
            })
        queue.put_nowait(_END)

    task.add_done_callback(on_finished)
    return _stream_events(queue)


async def _stream_events(queue: asyncio.Queue[dict | None]) -> AsyncIterator[str]:
    """Yield queued events as SSE until the run's final event."""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        except TimeoutError:
            yield ": heartbeat\n\n"
            continue
        if event is _END:
            return
        yield _sse(event)


async def _single_event(event: dict) -> AsyncIterator[str]:
    yield _sse(event)


def _blocking_run(
    state: AppState,
    dry_run: bool,
    rule_filter: str | None,
    trigger: str,
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> RunSummary:
    from em_phi.cli import _build_classifier, _build_provider

    # The config page can swap state.config mid-run; use one snapshot throughout.
    config = state.config
    # Recorded here in the worker thread: run-log writes stay off the event
    # loop, and the result is written before the stream's final event, so a
    # page reload after "done" already shows it. Errors are collected for the
    # run log and forwarded to the caller's on_error (scheduled runs pass none).
    recorder = RunRecorder(
        config.decision_log.path,
        trigger=trigger,
        dry_run=dry_run,
        rule_filter=rule_filter,
        on_error=on_error,
    )
    try:
        provider = _build_provider(config)
        provider.authenticate()
        classifier = _build_classifier(config)
        log = DecisionLog(config.decision_log.path)

        summary = process_all(
            config=config,
            provider=provider,
            classifier=classifier,
            log=log,
            dry_run=dry_run,
            rule_filter=rule_filter,
            on_email=on_email,
            on_error=recorder.errors,
        )
    except Exception as exc:
        recorder.fail(exc)
        raise
    recorder.finish(summary)
    return summary


# ------------------------------------------------------------------
# Status panel
# ------------------------------------------------------------------


@dataclass
class StatusCard:
    title: str
    state: str  # "ok" | "bad" | "neutral"
    summary: str
    details: list[str] = field(default_factory=list)
    hint: str | None = None


@dataclass
class RunStatus:
    gmail: StatusCard
    claude: StatusCard
    schedule: StatusCard
    last_run: RunRecord | None
    recent_runs: list[RunRecord]


def build_status(state: AppState, scheduler: EmPhiScheduler) -> RunStatus:
    """Context for the Run page's status cards and run history. No network calls."""
    config = state.config
    recent_runs, last_access = _load_runs(config.decision_log.path)
    last_run = recent_runs[0] if recent_runs else None
    return RunStatus(
        gmail=_gmail_card(state, last_run, last_access),
        claude=_claude_card(state),
        schedule=_schedule_card(scheduler),
        last_run=last_run,
        recent_runs=recent_runs,
    )


def _load_runs(path: Path) -> tuple[list[RunRecord], str | None]:
    # Don't create the database just to show an empty history.
    if not path.exists():
        return [], None
    try:
        run_log = RunLog(path)
        return run_log.recent(_RECENT_RUNS), run_log.last_successful_gmail_access()
    except Exception as exc:
        logger.warning("Could not read the run log: %s", exc)
        return [], None


def _gmail_card(state: AppState, last_run: RunRecord | None, last_access: str | None) -> StatusCard:
    from em_phi.providers.gmail import token_status

    ep = state.config.email_provider
    if ep.name != "gmail" or not ep.token_file:
        return StatusCard("Email provider", "neutral", f"{ep.name} — not checked")
    access = f"{last_access[:16]} UTC" if last_access else "never"
    details = [f"Last successful access: {access}"]
    token = token_status(ep.token_file)
    if (
        token.ok
        and last_run is not None
        and last_run.status == "failed"
        and "invalid_grant" in (last_run.failure or "")
    ):
        return StatusCard("Gmail", "bad", _INVALID_GRANT_SUMMARY, details)
    return StatusCard("Gmail", "ok" if token.ok else "bad", token.summary, details, token.hint)


def _claude_card(state: AppState) -> StatusCard:
    llm = state.config.llm
    if llm.name != "claude":
        return StatusCard("Classifier", "neutral", f"{llm.name} — not checked")
    details = [f"Model: {llm.model}"]
    # Only whether it is set; never any part of the key.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return StatusCard("Claude", "ok", "API key set", details)
    return StatusCard(
        "Claude", "bad", "ANTHROPIC_API_KEY not set", details,
        "Set it in the environment em-phi serve runs in",
    )


def _schedule_card(scheduler: EmPhiScheduler) -> StatusCard:
    description = scheduler.description()
    if description is None:
        return StatusCard("Schedule", "neutral", "Disabled")
    next_run = scheduler.next_run_time()
    details = []
    if next_run is not None:
        details.append(f"Next run: {next_run.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC")
    return StatusCard("Schedule", "ok", description[0].upper() + description[1:], details)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
