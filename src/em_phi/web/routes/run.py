from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.processor import OnEmail, OnError, RunSummary, process_all
from em_phi.web.state import AppState, LastRun

logger = logging.getLogger(__name__)

# Queue sentinel: the run task has finished and its final event is queued.
_END = None


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    @r.get("/run", response_class=HTMLResponse)
    async def run_page(request: Request):
        return templates.TemplateResponse(request, "run.html", {
            "rules": state.config.rules,
            "is_running": state.is_running,
            "last_run": state.last_run,
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
    task = await _start_run(state, dry_run, rule_filter)
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
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> asyncio.Task[RunSummary] | None:
    """Acquire the run lock and start a run in a worker thread.

    Returns None if a run is already in progress. Otherwise returns the run
    task, which holds the lock until the worker thread has finished and
    records state.last_run on success.
    """
    if state.run_lock.locked():
        return None
    # An uncontended asyncio.Lock.acquire() completes without suspending, so
    # there is no interleaving point between the check above and this line.
    await state.run_lock.acquire()

    loop = asyncio.get_running_loop()

    async def run() -> RunSummary:
        try:
            summary = await loop.run_in_executor(
                None, _blocking_run, state, dry_run, rule_filter, on_email, on_error
            )
            state.last_run = LastRun(
                summary=summary, dry_run=dry_run, finished_at=datetime.now(timezone.utc)
            )
            return summary
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
            "sender": email.sender,
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "action": action,
            "dry_run": is_dry,
        })

    def on_error(context: str, exc: Exception) -> None:
        emit({"type": "error", "context": context, "message": str(exc)})

    task = await _start_run(state, dry_run, rule_filter, on_email, on_error)
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
    on_email: OnEmail | None = None,
    on_error: OnError | None = None,
) -> RunSummary:
    from em_phi.cli import _build_classifier, _build_provider

    # The config page can swap state.config mid-run; use one snapshot throughout.
    config = state.config
    provider = _build_provider(config)
    provider.authenticate()
    classifier = _build_classifier(config)
    log = DecisionLog(config.decision_log.path)

    return process_all(
        config=config,
        provider=provider,
        classifier=classifier,
        log=log,
        dry_run=dry_run,
        rule_filter=rule_filter,
        on_email=on_email,
        on_error=on_error,
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
