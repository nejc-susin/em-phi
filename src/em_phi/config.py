from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(Exception):
    pass


# Top-level keys from the old config schema, mapped to their replacements.
_RENAMED_KEYS = {"gmail": "email_provider", "anthropic": "llm"}


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


class EmailProviderConfig(BaseModel):
    """Config for the email provider. Extra fields are preserved for custom providers."""

    model_config = ConfigDict(frozen=False, extra="allow")

    name: str = "gmail"
    # Gmail-specific fields; other providers define their own under email_provider:
    credentials_file: Path | None = None
    token_file: Path | None = None
    fetch_label: str | None = None  # if set, only fetch emails that carry this label
    unread_only: bool = True  # False: also process read emails from the last lookback_days
    lookback_days: int = Field(default=3, ge=1, le=30)  # only used when unread_only is false
    # Gmail web account used in links (/mail/u/<account>/): the signed-in account's index or its address
    account: str = "0"

    @field_validator("account", mode="before")
    @classmethod
    def account_as_str(cls, v: object) -> object:
        # `account: 1` in YAML is an int
        return str(v) if isinstance(v, int) and not isinstance(v, bool) else v

    @field_validator("credentials_file", "token_file", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path | None:
        if v is None:
            return None
        return _expand(str(v))


class LLMConfig(BaseModel):
    """Config for the LLM classifier. Extra fields are preserved for custom classifiers."""

    model_config = ConfigDict(extra="allow")

    name: str = "claude"
    model: str = "claude-haiku-5-5"
    max_tokens: int = 1024


class LabelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant: str = "EmPhi/Relevant"
    irrelevant: str = "EmPhi/Irrelevant"


class DecisionLogConfig(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    path: Path = Path("decisions.db")

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path:
        return _expand(str(v))


class LoggingConfig(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    file: Path | None = None

    @field_validator("file", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path | None:
        if v is None:
            return None
        return _expand(str(v))


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: list[str]
    name: str
    interests: str
    tolerance: Literal["aggressive", "balanced", "conservative"] = "balanced"
    action: Literal["label", "archive", "inbox"] = "label"

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [e.strip() for e in v.split(",") if e.strip()]
        if isinstance(v, list):
            return v
        raise ValueError("email must be a string or list of strings")

    @model_validator(mode="after")
    def check_non_empty(self) -> RuleConfig:
        if not self.interests.strip():
            raise ValueError(f"interests for rule '{self.email[0]}' must not be empty")
        return self


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_hours: int | None = Field(default=6, ge=1)
    cron: str | None = None  # takes precedence over interval_hours if set

    @field_validator("cron", mode="before")
    @classmethod
    def validate_cron(cls, v: object) -> str | None:
        if v is None or not str(v).strip():
            return None
        v = str(v).strip()
        # Validate here so a bad expression fails at load time, not when the
        # scheduler is (re)started. Imported lazily to keep config.py light.
        from apscheduler.triggers.cron import CronTrigger
        try:
            CronTrigger.from_crontab(v)
        except ValueError as e:
            raise ValueError(f"invalid cron expression {v!r}: {e}") from e
        return v


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8080
    auth_token: str  # required — no default; em-phi serve fails fast if absent


_MIN_AUTH_TOKEN_LENGTH = 12


def is_weak_auth_token(token: str) -> bool:
    """True for the example config's placeholder token or anything too short to resist guessing."""
    return token == "change-me" or len(token) < _MIN_AUTH_TOKEN_LENGTH


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    email_provider: EmailProviderConfig = EmailProviderConfig()
    llm: LLMConfig = LLMConfig()
    labels: LabelsConfig = LabelsConfig()
    decision_log: DecisionLogConfig = DecisionLogConfig()
    logging: LoggingConfig = LoggingConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    web: WebConfig | None = None
    rules: list[RuleConfig]

    @model_validator(mode="after")
    def check_rules(self) -> AppConfig:
        if not self.rules:
            raise ValueError("at least one rule must be configured")
        return self

    def resolve_relative_paths(self, config_dir: Path) -> None:
        """Resolve relative paths against the directory containing the config file."""
        ep = self.email_provider
        if ep.credentials_file and not ep.credentials_file.is_absolute():
            ep.credentials_file = config_dir / ep.credentials_file
        if ep.token_file and not ep.token_file.is_absolute():
            ep.token_file = config_dir / ep.token_file
        if not self.decision_log.path.is_absolute():
            self.decision_log.path = config_dir / self.decision_log.path
        if self.logging.file and not self.logging.file.is_absolute():
            self.logging.file = config_dir / self.logging.file


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open() as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a YAML mapping, got: {type(data).__name__}")

    try:
        config = AppConfig.model_validate(data)
    except ValidationError as e:
        messages = []
        for err in e.errors():
            loc = " -> ".join(str(p) for p in err["loc"]) if err["loc"] else "config"
            line = f"  {loc}: {err['msg']}"
            if err["type"] == "extra_forbidden" and len(err["loc"]) == 1 and err["loc"][0] in _RENAMED_KEYS:
                new = _RENAMED_KEYS[err["loc"][0]]
                line += f" (renamed to '{new}:', see config.example.yaml)"
            messages.append(line)
        raise ConfigError("Config validation failed:\n" + "\n".join(messages)) from e

    config.resolve_relative_paths(config_path.resolve().parent)
    return config
