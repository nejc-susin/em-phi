from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.decision_log import DecisionLog
from em_phi.web.state import AppState


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    @r.get("/log", response_class=HTMLResponse)
    async def log_page(
        request: Request,
        rule: str | None = None,
        days: int | None = None,
        limit: int = 50,
    ):
        log = DecisionLog(state.config.decision_log.path)
        entries = log.query(rule_email=rule, days=days, limit=limit)
        counts = log.count()
        known_rules = sorted({e for r in state.config.rules for e in r.email})

        return templates.TemplateResponse(request, "log.html", {
            "entries": entries,
            "counts": counts,
            "filter_rule": rule,
            "filter_days": days,
            "filter_limit": limit,
            "known_rules": known_rules,
        })

    return r
