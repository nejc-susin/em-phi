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


class GmailConfig(BaseModel):
    model_config = ConfigDict(frozen=False)

    credentials_file: Path
    token_file: Path

    @field_validator("credentials_file", "token_file", mode="before")
    @classmethod
    def expand_path(cls, v: object) -> Path:
        return _expand(str(v))


class AnthropicConfig(BaseModel):
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

    gmail: GmailConfig
    anthropic: AnthropicConfig = AnthropicConfig()
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
        if not self.gmail.credentials_file.is_absolute():
            self.gmail.credentials_file = config_dir / self.gmail.credentials_file
        if not self.gmail.token_file.is_absolute():
            self.gmail.token_file = config_dir / self.gmail.token_file
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
        # Reformat Pydantic errors into readable messages
        messages = []
        for err in e.errors():
            loc = " -> ".join(str(p) for p in err["loc"]) if err["loc"] else "config"
            messages.append(f"  {loc}: {err['msg']}")
        raise ConfigError("Config validation failed:\n" + "\n".join(messages)) from e

    config.resolve_relative_paths(config_path.resolve().parent)
    return config
