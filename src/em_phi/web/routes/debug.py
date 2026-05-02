from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.web.state import AppState


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    @r.get("/debug", response_class=HTMLResponse)
    async def debug_page(request: Request, rule: str | None = None, limit: int = 1):
        from em_phi.cli import _build_provider
        from em_phi.debug import fetch_debug_info

        infos = []
        error: str | None = None
        known_rules = sorted({e for r in state.config.rules for e in r.email})

        if rule:
            try:
                provider = _build_provider(state.config)
                provider.authenticate()
                infos = fetch_debug_info(
                    state.config, provider, rule_filter=rule, limit=limit
                )
            except Exception as exc:
                error = str(exc)

        return templates.TemplateResponse(request, "debug.html", {
            "infos": infos,
            "error": error,
            "selected_rule": rule,
            "limit": limit,
            "known_rules": known_rules,
        })

    return r
