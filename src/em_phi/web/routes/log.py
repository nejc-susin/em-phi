from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.decision_log import DecisionLog
from em_phi.web.state import AppState

_MIN_LIMIT = 1
_MAX_LIMIT = 500
_VALID_VERDICTS = {"relevant", "irrelevant"}
_VALID_ACTIONS = {"label", "archive", "keep"}


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    @r.get("/log", response_class=HTMLResponse)
    async def log_page(
        request: Request,
        rule: str | None = None,
        days: int | None = None,
        verdict: str | None = None,
        action: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        # Clamp/ignore rather than reject: this is a browsed page, not an API
        # — a stray query string shouldn't surface a raw validation error.
        limit = max(_MIN_LIMIT, min(limit, _MAX_LIMIT))
        offset = max(0, offset)
        if days is not None and days < 1:
            days = None
        if verdict not in _VALID_VERDICTS:
            verdict = None
        if action not in _VALID_ACTIONS:
            action = None
        search = search.strip() if search else None

        log = DecisionLog(state.config.decision_log.path)
        entries = log.query(
            rule_email=rule, days=days, verdict=verdict, action=action, search=search,
            limit=limit, offset=offset,
        )
        counts = log.count(rule_email=rule, days=days, verdict=verdict, action=action, search=search)
        known_rules = sorted({e for r in state.config.rules for e in r.email})
        total = sum(counts.values())

        return templates.TemplateResponse(request, "log.html", {
            "entries": entries,
            "counts": counts,
            "total": total,
            "filter_rule": rule,
            "filter_days": days,
            "filter_verdict": verdict,
            "filter_action": action,
            "filter_search": search,
            "filter_limit": limit,
            "filter_offset": offset,
            "known_rules": known_rules,
        })

    return r
