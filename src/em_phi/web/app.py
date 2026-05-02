from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from em_phi.config import AppConfig
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState, LastRun

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(config: AppConfig, config_path: Path) -> FastAPI:
    state = AppState(config, config_path)
    scheduler = EmPhiScheduler()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    async def scheduled_run() -> None:
        from em_phi.web.routes.run import execute_run
        await execute_run(state, dry_run=False, rule_filter=None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start(state.config, scheduled_run)
        yield
        scheduler.shutdown()

    app = FastAPI(title="em-phi", lifespan=lifespan)

    # ------------------------------------------------------------------ auth

    AUTH_COOKIE = "em_phi_auth"
    LOGIN_PATH = "/login"
    PUBLIC_PATHS = {LOGIN_PATH, "/login/submit"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        token = request.cookies.get(AUTH_COOKIE)
        if token != state.config.web.auth_token:  # type: ignore[union-attr]
            return RedirectResponse(url=LOGIN_PATH)
        return await call_next(request)

    @app.get(LOGIN_PATH, response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login/submit")
    async def login_submit(request: Request):
        form = await request.form()
        token = str(form.get("token", ""))
        if token == state.config.web.auth_token:  # type: ignore[union-attr]
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax")
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
    from em_phi.web.routes import debug as debug_routes
    from em_phi.web.routes import log as log_routes
    from em_phi.web.routes import run as run_routes

    app.include_router(config_routes.router(state, templates, scheduler))
    app.include_router(run_routes.router(state, templates))
    app.include_router(log_routes.router(state, templates))
    app.include_router(debug_routes.router(state, templates))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return RedirectResponse(url="/run")

    return app
