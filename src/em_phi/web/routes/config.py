from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from em_phi.config import AppConfig, RuleConfig, load_config
from em_phi.decision_log import DecisionLog
from em_phi.web.scheduler import EmPhiScheduler
from em_phi.web.state import AppState

logger = logging.getLogger(__name__)


def router(
    state: AppState,
    templates: Jinja2Templates,
    scheduler: EmPhiScheduler,
    scheduled_run: Callable[[], Awaitable[None]],
) -> APIRouter:
    r = APIRouter()

    @r.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        return templates.TemplateResponse(request, "config.html", {
            "config": state.config,
            "saved": request.query_params.get("saved"),
            "error": None,
        })

    @r.post("/config/rule/add")
    async def add_rule(request: Request):
        form = await request.form()
        error = None
        try:
            new_rule = RuleConfig(
                email=_parse_emails(str(form.get("email", ""))),
                name=str(form.get("name", "")).strip(),
                interests=str(form.get("interests", "")).strip(),
                tolerance=str(form.get("tolerance", "balanced")),
                action=str(form.get("action", "label")),
            )
            config = state.config.model_copy(deep=True)
            config.rules = [*config.rules, new_rule]
            _save_and_reload(state, config, scheduler, scheduled_run)
        except Exception as exc:
            error = str(exc)
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": error,
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/rule/{index}/edit")
    async def edit_rule(request: Request, index: int):
        form = await request.form()
        error = None
        try:
            rules = list(state.config.rules)
            if index < 0 or index >= len(rules):
                raise ValueError(f"Invalid rule index {index}")
            rules[index] = RuleConfig(
                email=_parse_emails(str(form.get("email", ""))),
                name=str(form.get("name", "")).strip(),
                interests=str(form.get("interests", "")).strip(),
                tolerance=str(form.get("tolerance", "balanced")),
                action=str(form.get("action", "label")),
            )
            config = state.config.model_copy(deep=True)
            config.rules = rules
            _save_and_reload(state, config, scheduler, scheduled_run)
        except Exception as exc:
            error = str(exc)
            return templates.TemplateResponse(request, "config.html", {
                "config": state.config,
                "saved": None,
                "error": error,
            }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/rule/{index}/delete")
    async def delete_rule(request: Request, index: int):
        rules = list(state.config.rules)
        if 0 <= index < len(rules):
            rules.pop(index)
            config = state.config.model_copy(deep=True)
            config.rules = rules
            try:
                _save_and_reload(state, config, scheduler, scheduled_run)
            except Exception as exc:
                return templates.TemplateResponse(request, "config.html", {
                    "config": state.config,
                    "saved": None,
                    "error": str(exc),
                }, status_code=422)
        return RedirectResponse(url="/config?saved=1", status_code=303)

    @r.post("/config/settings")
    async def save_settings(request: Request):
        form = await request.form()
        try:
            from em_phi.config import DecisionLogConfig, LabelsConfig, LoggingConfig
            config = state.config.model_copy(deep=True)
            # model_copy keeps llm.name (and any extra fields) intact
            config.llm = config.llm.model_copy(update={
                "model": str(form.get("model", config.llm.model)).strip(),
                "max_tokens": int(form.get("max_tokens") or 1024),
            })
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
            # An unchecked checkbox is simply absent from the form
            config.email_provider.unread_only = form.get("unread_only") == "on"
            config.email_provider.lookback_days = int(
                form.get("lookback_days") or config.email_provider.lookback_days
            )
            _save_and_reload(state, config, scheduler, scheduled_run)
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
            config = state.config.model_copy(deep=True)
            config.schedule = sc
            _save_and_reload(state, config, scheduler, scheduled_run)
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


def _save_and_reload(
    state: AppState,
    config: AppConfig,
    scheduler: EmPhiScheduler,
    scheduled_run: Callable[[], Awaitable[None]],
) -> None:
    """Validate and atomically write config to disk, then swap it into state.

    The YAML goes to a temp file next to config.yaml and is loaded back with
    load_config before it replaces the real file, so an invalid config never
    reaches disk and the in-memory config only changes on success.
    """
    path = state.config_path
    config_dir = path.resolve().parent
    fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # config.yaml holds web.auth_token — keep its permissions.
        if path.exists():
            shutil.copymode(path, tmp)
        _write_yaml(config, tmp, config_dir)
        reloaded = load_config(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    state.reload_config(reloaded)

    scheduler.reschedule(reloaded, scheduled_run)
    # Newly added rules claim the old rows from their senders.
    try:
        if reloaded.decision_log.path.exists():  # nothing to backfill before the first run
            DecisionLog(reloaded.decision_log.path).backfill_rule_names(reloaded.rules)
    except Exception as exc:
        logger.warning("Could not backfill rule names in the decision log: %s", exc)
    logger.info("Config saved and reloaded: %s", state.config_path)


def _yaml_path(p: Path, config_dir: Path) -> str:
    """Paths inside the config dir are written relative to it, others absolute."""
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(config_dir).as_posix()
    except ValueError:
        return str(p)


def _write_yaml(config: AppConfig, path: Path, config_dir: Path) -> None:
    """Serialize AppConfig to YAML at path using ruamel.yaml."""
    from ruamel.yaml import YAML

    data: dict = {
        "email_provider": {
            "name": config.email_provider.name,
        },
        "llm": {
            "name": config.llm.name,
            "model": config.llm.model,
            "max_tokens": config.llm.max_tokens,
        },
        "labels": {
            "relevant": config.labels.relevant,
            "irrelevant": config.labels.irrelevant,
        },
        "decision_log": {
            "path": _yaml_path(config.decision_log.path, config_dir),
        },
        "logging": {
            "level": config.logging.level,
        },
        "schedule": {
            "enabled": config.schedule.enabled,
        },
        "rules": [
            {
                "email": r.email if len(r.email) > 1 else r.email[0],
                "name": r.name,
                "interests": r.interests,
                "tolerance": r.tolerance,
                "action": r.action,
            }
            for r in config.rules
        ],
    }

    # Include optional fields
    ep = config.email_provider
    if ep.credentials_file:
        data["email_provider"]["credentials_file"] = _yaml_path(ep.credentials_file, config_dir)
    if ep.token_file:
        data["email_provider"]["token_file"] = _yaml_path(ep.token_file, config_dir)
    if ep.fetch_label:
        data["email_provider"]["fetch_label"] = ep.fetch_label
    if not ep.unread_only:
        data["email_provider"]["unread_only"] = False
    if ep.lookback_days != 3:
        data["email_provider"]["lookback_days"] = ep.lookback_days
    if ep.account != "0":
        data["email_provider"]["account"] = ep.account
    # Provider- and classifier-specific fields the models don't know about
    data["email_provider"].update(ep.model_extra or {})
    data["llm"].update(getattr(config.llm, "model_extra", None) or {})
    if config.logging.file:
        data["logging"]["file"] = _yaml_path(config.logging.file, config_dir)
    if config.schedule.interval_hours is not None:
        data["schedule"]["interval_hours"] = config.schedule.interval_hours
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

    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
