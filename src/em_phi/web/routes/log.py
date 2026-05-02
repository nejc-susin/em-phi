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
        sender: str | None = None,
        days: int | None = None,
        limit: int = 50,
    ):
        log = DecisionLog(state.config.decision_log.path)
        entries = log.query(sender=sender, days=days, limit=limit)
        counts = log.count()
        known_senders = sorted({e for s in state.config.senders for e in s.email})

        return templates.TemplateResponse(request, "log.html", {
            "entries": entries,
            "counts": counts,
            "filter_sender": sender,
            "filter_days": days,
            "filter_limit": limit,
            "known_senders": known_senders,
        })

    return r
