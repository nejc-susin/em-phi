from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from em_phi.config import AppConfig, is_weak_auth_token
from em_phi.decision_log import DecisionLog
from em_phi.run_log import RunLog
from em_phi.web.routes._helpers import message_link
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState

logger = logging.getLogger(__name__)

# Sec-Fetch-Site is sent by modern browsers on every request. Anything other
# than same-origin/none (typed URL, bookmark) is a cross-site request.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SAFE_FETCH_SITES = {"same-origin", "none"}
# GET /run/stream starts a real run that labels/archives mail. Lax cookies are
# sent on cross-site top-level GET navigations, so it needs the check too.
_CROSS_SITE_CHECKED_GET_PATHS = {"/run/stream"}


def _session_value(token: str) -> str:
    """Cookie value derived from the auth token, so the raw token never leaves the server."""
    return hmac.new(token.encode(), b"em-phi-session", hashlib.sha256).hexdigest()


def _safe_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: AppConfig, config_path: Path) -> FastAPI:
    state = AppState(config, config_path)
    scheduler = EmPhiScheduler()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # A callable (not a value) so the banner follows config reloads.
    templates.env.globals["weak_auth_token"] = lambda: is_weak_auth_token(state.config.web.auth_token)  # type: ignore[union-attr]
    templates.env.globals["gmail_url"] = lambda message_id: message_link(state.config, message_id)

    async def scheduled_run() -> None:
        from em_phi.web.routes.run import execute_run
        await execute_run(state, dry_run=False, rule_filter=None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            DecisionLog(state.config.decision_log.path).backfill_rule_names(state.config.rules)
        except Exception as exc:
            logger.warning("Could not backfill rule names in the decision log: %s", exc)
        try:
            RunLog(state.config.decision_log.path).mark_interrupted()
        except Exception as exc:
            logger.warning("Could not mark interrupted runs in the run log: %s", exc)
        scheduler.start(state.config, scheduled_run)
        yield
        scheduler.shutdown()

    app = FastAPI(title="em-phi", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ auth

    AUTH_COOKIE = "em_phi_auth"
    LOGIN_PATH = "/login"
    PUBLIC_PATHS = {LOGIN_PATH, "/login/submit"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Static assets (vendored JS, no external CDN calls) must load
        # before the user has a valid auth cookie — the login page itself
        # needs them.
        path = request.url.path
        fetch_site = request.headers.get("sec-fetch-site")
        if (
            fetch_site is not None
            and fetch_site not in _SAFE_FETCH_SITES
            and (request.method not in _SAFE_METHODS or path in _CROSS_SITE_CHECKED_GET_PATHS)
        ):
            return Response("Cross-site request blocked", status_code=403)
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        cookie = request.cookies.get(AUTH_COOKIE, "")
        expected = _session_value(state.config.web.auth_token)  # type: ignore[union-attr]
        if not _safe_equals(cookie, expected):
            return RedirectResponse(url=LOGIN_PATH)
        return await call_next(request)

    @app.get(LOGIN_PATH, response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login/submit")
    async def login_submit(request: Request):
        form = await request.form()
        token = str(form.get("token", ""))
        if _safe_equals(token, state.config.web.auth_token):  # type: ignore[union-attr]
            response = RedirectResponse(url="/", status_code=303)
            # Behind a TLS-terminating proxy, uvicorn needs --proxy-headers
            # (forwarded headers) for the scheme to be https here.
            response.set_cookie(
                AUTH_COOKIE,
                _session_value(token),
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
            return response
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid token."}, status_code=401
        )

    @app.post("/logout")
    async def logout():
        response = RedirectResponse(url=LOGIN_PATH, status_code=303)
        response.delete_cookie(AUTH_COOKIE)
        return response

    # ------------------------------------------------------------------ routes

    from em_phi.web.routes import config as config_routes
    from em_phi.web.routes import log as log_routes
    from em_phi.web.routes import playground as playground_routes
    from em_phi.web.routes import run as run_routes

    app.include_router(config_routes.router(state, templates, scheduler, scheduled_run))
    app.include_router(run_routes.router(state, templates, scheduler))
    app.include_router(log_routes.router(state, templates))
    app.include_router(playground_routes.router(state, templates, scheduler, scheduled_run))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return RedirectResponse(url="/run")

    return app
