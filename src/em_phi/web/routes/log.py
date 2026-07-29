from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.decision_log import DecisionLog
from em_phi.web.state import AppState

_MIN_LIMIT = 1
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50
_VALID_VERDICTS = {"relevant", "irrelevant"}
_VALID_ACTIONS = {"label", "archive", "keep"}


def _parse_int(raw: str | None) -> int | None:
    """Parse a query-string int, treating blank/invalid input as absent rather than erroring."""
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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
        parsed_days = _parse_int(days)
        days = parsed_days if (parsed_days is not None and parsed_days >= 1) else None
        limit = max(_MIN_LIMIT, min(_parse_int(limit) or _DEFAULT_LIMIT, _MAX_LIMIT))
        offset = max(0, _parse_int(offset) or 0)
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
        rule_options = sorted(
            {(e, r.name) for r in state.config.rules for e in r.email},
            key=lambda pair: pair[0],
        )
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
            "rule_options": rule_options,
        })

    return r
