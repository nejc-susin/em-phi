from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from em_phi.config import AppConfig, RuleConfig
from em_phi.decision_log import DecisionLog
from em_phi.playground import PlaygroundResult, run_playground, trial_rule
from em_phi.web.routes._helpers import parse_int
from em_phi.web.routes.config import _save_and_reload
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState

logger = logging.getLogger(__name__)

MIN_DAYS, MAX_DAYS, DEFAULT_DAYS = 1, 90, 14
MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT = 1, 20, 10

STALE_RULE_ERROR = "The rules changed since this page was loaded — reload and try again."
_PROMPTS_ONLY_HINT = " Tick “Prompts only” to inspect the prompts without calling the classifier."


def _clamp(raw: str | None, lo: int, hi: int, default: int) -> int:
    value = parse_int(raw)
    if value is None:
        return default
    return max(lo, min(value, hi))


def _form_values(
    rule_index: str, rule_name: str, interests: str, tolerance: str,
    days: str, limit: str, prompts_only: str | None,
) -> dict:
    """The submitted form, normalised for re-rendering (the user's edits, not the saved config)."""
    return {
        "rule_index": parse_int(rule_index),
        "rule_name": rule_name,
        "interests": interests,
        "tolerance": tolerance,
        "days": _clamp(days, MIN_DAYS, MAX_DAYS, DEFAULT_DAYS),
        "limit": _clamp(limit, MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT),
        "prompts_only": prompts_only is not None,
    }


def _resolve_rule(config: AppConfig, rule_index: str | None, rule_name: str) -> tuple[int, RuleConfig]:
    """The rule the form was loaded for; ValueError if the index/name no longer match the config."""
    index = parse_int(rule_index)
    if index is None or not 0 <= index < len(config.rules) or config.rules[index].name != rule_name:
        raise ValueError(STALE_RULE_ERROR)
    return index, config.rules[index]


def _summary(result: PlaygroundResult) -> dict[str, int]:
    verdicts = [i.verdict.verdict for i in result.items if i.verdict is not None]
    return {
        "total": len(result.items),
        "changed": sum(1 for i in result.items if i.changed),
        "relevant": verdicts.count("relevant"),
        "irrelevant": verdicts.count("irrelevant"),
        "errors": sum(1 for i in result.items if i.error),
    }


def router(
    state: AppState,
    templates: Jinja2Templates,
    scheduler: EmPhiScheduler,
    scheduled_run: Callable[[], Awaitable[None]],
) -> APIRouter:
    r = APIRouter()

    def render(
        request: Request,
        *,
        rule_index: int | None,
        rule_name: str,
        interests: str,
        tolerance: str,
        days: int = DEFAULT_DAYS,
        limit: int = DEFAULT_LIMIT,
        prompts_only: bool = False,
        result: PlaygroundResult | None = None,
        error: str | None = None,
        saved: bool = False,
        status_code: int = 200,
    ):
        config = state.config
        return templates.TemplateResponse(request, "playground.html", {
            "rules": [(i, rule.name, ", ".join(rule.email)) for i, rule in enumerate(config.rules)],
            "rule_index": rule_index,
            "rule_name": rule_name,
            "interests": interests,
            "tolerance": tolerance,
            "days": days,
            "limit": limit,
            "prompts_only": prompts_only,
            "result": result,
            "summary": _summary(result) if result is not None else None,
            "error": error,
            "saved": saved,
            "min_days": MIN_DAYS, "max_days": MAX_DAYS,
            "min_limit": MIN_LIMIT, "max_limit": MAX_LIMIT,
        }, status_code=status_code)

    @r.get("/debug")
    async def debug_redirect():
        # The old prompt inspector lives on as the playground's "prompts only" mode.
        return RedirectResponse(url="/playground", status_code=307)

    @r.get("/playground", response_class=HTMLResponse)
    async def playground_page(request: Request, rule: str | None = None, saved: str | None = None):
        rules = state.config.rules
        index = parse_int(rule)
        if index is None or not 0 <= index < len(rules):
            index = 0
        selected = rules[index]
        return render(
            request,
            rule_index=index,
            rule_name=selected.name,
            interests=selected.interests,
            tolerance=selected.tolerance,
            saved=bool(saved),
        )

    # Plain `def` (not `async def`) on purpose: it does blocking Gmail and
    # Claude network I/O, so FastAPI must run it in its threadpool rather than
    # on the event loop (which would freeze SSE streams, other requests and
    # the scheduler).
    @r.post("/playground/test", response_class=HTMLResponse)
    def playground_test(
        request: Request,
        rule_index: Annotated[str, Form()] = "",
        rule_name: Annotated[str, Form()] = "",
        interests: Annotated[str, Form()] = "",
        tolerance: Annotated[str, Form()] = "balanced",
        days: Annotated[str, Form()] = "",
        limit: Annotated[str, Form()] = "",
        prompts_only: Annotated[str | None, Form()] = None,
    ):
        from em_phi.cli import _build_classifier, _build_provider

        config = state.config  # snapshot: a concurrent config save swaps state.config
        form = _form_values(rule_index, rule_name, interests, tolerance, days, limit, prompts_only)

        try:
            _, rule = _resolve_rule(config, rule_index, rule_name)
            trial = trial_rule(rule, interests, tolerance)
        except ValueError as exc:
            return render(request, **form, error=str(exc), status_code=422)

        classifier = None
        if not form["prompts_only"]:
            try:
                classifier = _build_classifier(config)
            except Exception as exc:
                return render(request, **form, error=f"{exc}{_PROMPTS_ONLY_HINT}")

        try:
            provider = _build_provider(config)
            provider.authenticate()
            # Don't create the database just to look up decisions.
            log_path = config.decision_log.path
            log = DecisionLog(log_path) if log_path.exists() else None
            result = run_playground(
                provider=provider,
                classifier=classifier,
                log=log,
                rule=trial,
                days=form["days"],
                limit=form["limit"],
            )
        except Exception as exc:
            logger.warning("Playground test failed: %s", exc)
            return render(request, **form, error=str(exc))

        return render(request, **form, result=result)

    @r.post("/playground/save")
    async def playground_save(
        request: Request,
        rule_index: Annotated[str, Form()] = "",
        rule_name: Annotated[str, Form()] = "",
        interests: Annotated[str, Form()] = "",
        tolerance: Annotated[str, Form()] = "balanced",
        days: Annotated[str, Form()] = "",
        limit: Annotated[str, Form()] = "",
        prompts_only: Annotated[str | None, Form()] = None,
    ):
        form = _form_values(rule_index, rule_name, interests, tolerance, days, limit, prompts_only)
        try:
            index, rule = _resolve_rule(state.config, rule_index, rule_name)
            config = state.config.model_copy(deep=True)
            config.rules[index] = trial_rule(rule, interests.strip(), tolerance)
            _save_and_reload(state, config, scheduler, scheduled_run)
        except Exception as exc:
            return render(request, **form, error=str(exc), status_code=422)
        return RedirectResponse(url=f"/playground?rule={index}&saved=1", status_code=303)

    return r
