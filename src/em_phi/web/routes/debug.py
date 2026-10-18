from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.web.routes._helpers import parse_int, rule_options
from em_phi.web.state import AppState

_MIN_LIMIT = 1
_MAX_LIMIT = 10
_DEFAULT_LIMIT = 1


def router(state: AppState, templates: Jinja2Templates) -> APIRouter:
    r = APIRouter()

    # Plain `def` (not `async def`) on purpose: it does blocking Gmail network
    # I/O, so FastAPI must run it in its threadpool rather than on the event
    # loop (which would freeze SSE streams, other requests and the scheduler).
    @r.get("/debug", response_class=HTMLResponse)
    def debug_page(request: Request, rule: str | None = None, limit: str | None = None):
        from em_phi.cli import _build_provider
        from em_phi.debug import fetch_debug_info

        # Taken as a raw string and parsed by hand — this is a browsed page,
        # not an API, so a blank/malformed Limit field shouldn't surface
        # FastAPI's raw int-coercion validation error.
        limit = max(_MIN_LIMIT, min(parse_int(limit) or _DEFAULT_LIMIT, _MAX_LIMIT))

        infos = []
        error: str | None = None
        options = rule_options(state.config)

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
            "rule_options": options,
        })

    return r
