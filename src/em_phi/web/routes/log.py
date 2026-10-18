from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.actions import ACTIONS_TAKEN
from em_phi.decision_log import DecisionLog
from em_phi.web.routes._helpers import parse_int, rule_options
from em_phi.web.state import AppState

_MIN_LIMIT = 1
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50
_VALID_VERDICTS = {"relevant", "irrelevant"}


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    @r.get("/log", response_class=HTMLResponse)
    async def log_page(
        request: Request,
        rule: str | None = None,
        days: str | None = None,
        verdict: str | None = None,
        action: str | None = None,
        search: str | None = None,
        limit: str | None = None,
        offset: str | None = None,
    ):
        # Query params are taken as raw strings and parsed by hand — this is a
        # browsed page, not an API, so a blank or malformed field (e.g. the
        # Days box left empty) should be ignored, not surface a raw
        # validation error from FastAPI's own int coercion.
        parsed_days = parse_int(days)
        days = parsed_days if (parsed_days is not None and parsed_days >= 1) else None
        limit = max(_MIN_LIMIT, min(parse_int(limit) or _DEFAULT_LIMIT, _MAX_LIMIT))
        offset = max(0, parse_int(offset) or 0)
        if verdict not in _VALID_VERDICTS:
            verdict = None
        if action not in ACTIONS_TAKEN:
            action = None
        search = search.strip() if search else None

        log = DecisionLog(state.config.decision_log.path)
        entries = log.query(
            rule_email=rule, days=days, verdict=verdict, action=action, search=search,
            limit=limit, offset=offset,
        )
        counts = log.count(rule_email=rule, days=days, verdict=verdict, action=action, search=search)
        options = rule_options(state.config)
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
            "rule_options": options,
            "action_options": ACTIONS_TAKEN,
        })

    return r
