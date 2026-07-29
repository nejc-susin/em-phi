from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from em_phi.web.state import AppState

_MIN_LIMIT = 1
_MAX_LIMIT = 10
_DEFAULT_LIMIT = 1


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

    @r.get("/debug", response_class=HTMLResponse)
    async def debug_page(request: Request, rule: str | None = None, limit: str | None = None):
        from em_phi.cli import _build_provider
        from em_phi.debug import fetch_debug_info

        # Taken as a raw string and parsed by hand — this is a browsed page,
        # not an API, so a blank/malformed Limit field shouldn't surface
        # FastAPI's raw int-coercion validation error.
        limit = max(_MIN_LIMIT, min(_parse_int(limit) or _DEFAULT_LIMIT, _MAX_LIMIT))

        infos = []
        error: str | None = None
        rule_options = sorted(
            {(e, r.name) for r in state.config.rules for e in r.email},
            key=lambda pair: pair[0],
        )

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
            "rule_options": rule_options,
        })

    return r
