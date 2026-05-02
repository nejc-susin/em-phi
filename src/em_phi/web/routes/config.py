from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from em_phi.config import AppConfig, ConfigError, SenderConfig, load_config
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState

logger = logging.getLogger(__name__)


def router(state: AppState, templates: Jinja2Templates, scheduler: EmPhiScheduler) -> APIRouter:
    r = APIRouter()

    @r.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        return templates.TemplateResponse(request, "config.html", {
            "config": state.config,
            "saved": request.query_params.get("saved"),
            "error": None,
        })

    @r.post("/config/sender/add")
    async def add_sender(request: Request):
        form = await request.form()
        error = None
        try:
            new_sender = SenderConfig(
                email=_parse_emails(str(form.get("email", ""))),
                name=str(form.get("name", "")).strip(),
                interests=str(form.get("interests", "")).strip(),
                tolerance=str(form.get("tolerance", "balanced")),
                action=str(form.get("action", "label")),
            )
            config = state.config
            config.senders = [*config.senders, new_sender]
            _save_and_reload(state, config, scheduler)
        except Exception as exc:
            error = str(exc)
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": error,
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/sender/{index}/edit")
    async def edit_sender(request: Request, index: int):
        form = await request.form()
        error = None
        try:
            senders = list(state.config.senders)
            if index < 0 or index >= len(senders):
                raise ValueError(f"Invalid sender index {index}")
            senders[index] = SenderConfig(
                email=str(form.get("email", "")).strip(),
                name=str(form.get("name", "")).strip(),
                interests=str(form.get("interests", "")).strip(),
                tolerance=str(form.get("tolerance", "balanced")),
                action=str(form.get("action", "label")),
            )
            config = state.config
            config.senders = senders
            _save_and_reload(state, config, scheduler)
        except Exception as exc:
            error = str(exc)
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": error,
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/sender/{index}/delete")
    async def delete_sender(request: Request, index: int):
        senders = list(state.config.senders)
        if 0 <= index < len(senders):
            senders.pop(index)
            config = state.config
            config.senders = senders
            _save_and_reload(state, config, scheduler)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/settings")
    async def save_settings(request: Request):
        form = await request.form()
        try:
            from em_phi.config import DecisionLogConfig, LabelsConfig, LLMConfig, LoggingConfig
            config = state.config
            config.llm = LLMConfig(
                name=config.llm.name,
                model=str(form.get("model", config.llm.model)).strip(),
                max_tokens=int(form.get("max_tokens") or 256),
            )
            config.labels = LabelsConfig(
                relevant=str(form.get("labels_relevant", "EmPhi/Relevant")).strip(),
                irrelevant=str(form.get("labels_irrelevant", "EmPhi/Irrelevant")).strip(),
            )
            config.decision_log = DecisionLogConfig(
                path=str(form.get("decision_log_path", str(config.decision_log.path))).strip()
            )
            config.logging = LoggingConfig(
                level=str(form.get("log_level", "WARNING")),  # type: ignore[arg-type]
                file=config.logging.file,
            )
            fetch_label = str(form.get("fetch_label", "")).strip() or None
            config.email_provider.fetch_label = fetch_label
            _save_and_reload(state, config, scheduler)
        except Exception as exc:
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": str(exc),
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/schedule")
    async def save_schedule(request: Request):
        form = await request.form()
        error = None
        try:
            from em_phi.config import ScheduleConfig
            sc = ScheduleConfig(
                enabled=form.get("enabled") == "on",
                interval_hours=int(form.get("interval_hours") or 6),
                cron=str(form.get("cron", "")).strip() or None,
            )
            config = state.config
            config.schedule = sc
            _save_and_reload(state, config, scheduler)
        except Exception as exc:
            error = str(exc)
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": error,
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    return r


def _parse_emails(raw: str) -> list[str]:
    return [e.strip() for e in raw.split(",") if e.strip()]


def _save_and_reload(state: AppState, config: AppConfig, scheduler: EmPhiScheduler) -> None:
    """Write config to disk (YAML) and reload state."""
    _write_yaml(config, state.config_path)
    reloaded = load_config(state.config_path)
    state.reload_config(reloaded)

    async def scheduled_run() -> None:
        from em_phi.web.routes.run import execute_run
        await execute_run(state, dry_run=False, sender_filter=None)

    scheduler.reschedule(reloaded, scheduled_run)
    logger.info("Config saved and reloaded: %s", state.config_path)


def _write_yaml(config: AppConfig, path: Path) -> None:
    """Serialize AppConfig back to YAML using ruamel.yaml and write atomically."""
    from ruamel.yaml import YAML

    data: dict = {
        "email_provider": {
            "name": config.email_provider.name,
        },
        "llm": {
            "model": config.llm.model,
            "max_tokens": config.llm.max_tokens,
        },
        "labels": {
            "relevant": config.labels.relevant,
            "irrelevant": config.labels.irrelevant,
        },
        "decision_log": {
            "path": str(config.decision_log.path),
        },
        "logging": {
            "level": config.logging.level,
        },
        "schedule": {
            "enabled": config.schedule.enabled,
            "interval_hours": config.schedule.interval_hours,
        },
        "senders": [
            {
                "email": s.email if len(s.email) > 1 else s.email[0],
                "name": s.name,
                "interests": s.interests,
                "tolerance": s.tolerance,
                "action": s.action,
            }
            for s in config.senders
        ],
    }

    # Include optional fields
    ep = config.email_provider
    if ep.credentials_file:
        data["email_provider"]["credentials_file"] = str(ep.credentials_file)
    if ep.token_file:
        data["email_provider"]["token_file"] = str(ep.token_file)
    if ep.fetch_label:
        data["email_provider"]["fetch_label"] = ep.fetch_label
    if config.logging.file:
        data["logging"]["file"] = str(config.logging.file)
    if config.schedule.cron:
        data["schedule"]["cron"] = config.schedule.cron
    if config.web:
        data["web"] = {
            "host": config.web.host,
            "port": config.web.port,
            "auth_token": config.web.auth_token,
        }

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 120

    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    tmp.rename(path)
