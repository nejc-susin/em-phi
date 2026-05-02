from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from em_phi.models import Email, Verdict
from em_phi.processor import RunSummary, _process_rule
from em_phi.web.state import AppState, LastRun

logger = logging.getLogger(__name__)


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
        if state.is_running:
            raise HTTPException(409, "A run is already in progress")

        return StreamingResponse(
            _run_generator(state, dry_run=dry_run, rule_filter=rule),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return r


async def execute_run(
    state: AppState,
    dry_run: bool,
    rule_filter: str | None,
) -> RunSummary:
    """Run process_all in a thread pool, streaming progress via callbacks.

    Called by both the SSE route and the scheduler.
    """
    async with state.run_lock:
        state.is_running = True
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _blocking_run(state, dry_run, rule_filter, queue=None),
            )
        finally:
            state.is_running = False


async def _run_generator(state: AppState, dry_run: bool, rule_filter: str | None):
    """Async generator that yields SSE events while processing emails."""
    if state.run_lock.locked():
        yield _sse({"type": "error", "message": "A run is already in progress"})
        return

    queue: asyncio.Queue[dict] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_email(email: Email, verdict: Verdict, action: str, is_dry: bool) -> None:
        asyncio.run_coroutine_threadsafe(
            queue.put({
                "type": "verdict",
                "subject": email.subject,
                "sender": email.sender,
                "verdict": verdict.verdict,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "action": action,
                "dry_run": is_dry,
            }),
            loop,
        )

    def on_error(context: str, exc: Exception) -> None:
        asyncio.run_coroutine_threadsafe(
            queue.put({"type": "error", "context": context, "message": str(exc)}),
            loop,
        )

    async with state.run_lock:
        state.is_running = True
        try:
            future = loop.run_in_executor(
                None,
                lambda: _blocking_run(state, dry_run, rule_filter, queue, on_email, on_error, loop),
            )

            while not future.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

            summary: RunSummary = await future
            state.last_run = LastRun(
                summary=summary, dry_run=dry_run, finished_at=datetime.now()
            )
            yield _sse({
                "type": "done",
                "processed": summary.processed,
                "relevant": summary.relevant,
                "irrelevant": summary.irrelevant,
                "skipped": summary.skipped,
                "errors": summary.errors,
            })
        except Exception as exc:
            logger.error("Run failed: %s", exc)
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            state.is_running = False


def _blocking_run(
    state: AppState,
    dry_run: bool,
    rule_filter: str | None,
    queue,
    on_email=None,
    on_error=None,
    loop=None,
) -> RunSummary:
    from em_phi.cli import _build_classifier, _build_provider
    from em_phi.decision_log import DecisionLog
    from em_phi.processor import RunSummary, _process_rule

    config = state.config
    provider = _build_provider(config)
    provider.authenticate()
    classifier = _build_classifier(config)
    log = DecisionLog(config.decision_log.path)

    rules = config.rules
    if rule_filter:
        rules = [r for r in rules if rule_filter in r.email]

    summary = RunSummary()
    for r in rules:
        result = _process_rule(
            rule=r,
            config=config,
            provider=provider,
            classifier=classifier,
            log=log,
            dry_run=dry_run,
            on_email=on_email,
            on_error=on_error,
        )
        summary.results.append(result)

    return summary


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
