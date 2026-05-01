from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator, ValidationError


class ConfigError(Exception):
    pass


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


class EmailProviderConfig(BaseModel):
    """Config for the email provider. Extra fields are preserved for custom providers."""

    model_config = ConfigDict(frozen=False, extra="allow")

    name: str = "gmail"
    # Gmail-specific fields; other providers define their own under email_provider:
    credentials_file: Path | None = None
    token_file: Path | None = None

    @field_validator("credentials_file", "token_file", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path | None:
        if v is None:
            return None
        return _expand(str(v))


class LLMConfig(BaseModel):
    """Config for the LLM classifier."""

    name: str = "claude"
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 256


class LabelsConfig(BaseModel):
    relevant: str = "EmPhi/Relevant"
    irrelevant: str = "EmPhi/Irrelevant"


class DecisionLogConfig(BaseModel):
    model_config = ConfigDict(frozen=False)

    path: Path = Path("decisions.db")

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path:
        return _expand(str(v))


class SenderConfig(BaseModel):
    email: str
    name: str
    interests: str
    tolerance: Literal["aggressive", "balanced", "conservative"] = "balanced"
    action: Literal["label", "archive"] = "label"

    @model_validator(mode="after")
    def check_non_empty(self) -> SenderConfig:
        if not self.interests.strip():
            raise ValueError(f"interests for sender '{self.email}' must not be empty")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=False)

    email_provider: EmailProviderConfig = EmailProviderConfig()
    llm: LLMConfig = LLMConfig()
    labels: LabelsConfig = LabelsConfig()
    decision_log: DecisionLogConfig = DecisionLogConfig()
    senders: list[SenderConfig]

    @model_validator(mode="after")
    def check_senders(self) -> AppConfig:
        if not self.senders:
            raise ValueError("at least one sender must be configured")
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
            messages.append(f"  {loc}: {err['msg']}")
        raise ConfigError("Config validation failed:\n" + "\n".join(messages)) from e

    config.resolve_relative_paths(config_path.resolve().parent)
    return config
