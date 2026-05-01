import os
from pathlib import Path

import pytest
import yaml

from em_phi.config import ConfigError, load_config


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(data))
    return p


_VALID_DATA = {
    "gmail": {"credentials_file": "credentials.json", "token_file": "token.json"},
    "senders": [
        {
            "email": "news@example.com",
            "name": "Example",
            "interests": "Python news",
            "tolerance": "balanced",
            "action": "label",
        }
    ],
}


def test_valid_config_loads(tmp_path: Path) -> None:
    p = _write_config(tmp_path, _VALID_DATA)
    config = load_config(p)
    assert config.senders[0].email == "news@example.com"
    assert config.anthropic.model == "claude-haiku-4-5-20251001"


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/config.yaml"))


def test_bad_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("not: [valid: yaml")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(p)


def test_missing_senders_raises(tmp_path: Path) -> None:
    data = {k: v for k, v in _VALID_DATA.items() if k != "senders"}
    p = _write_config(tmp_path, data)
    with pytest.raises(ConfigError, match="senders"):
        load_config(p)


def test_empty_senders_raises(tmp_path: Path) -> None:
    data = {**_VALID_DATA, "senders": []}
    p = _write_config(tmp_path, data)
    with pytest.raises(ConfigError, match="at least one sender"):
        load_config(p)


def test_empty_interests_raises(tmp_path: Path) -> None:
    data = {
        **_VALID_DATA,
        "senders": [{**_VALID_DATA["senders"][0], "interests": "   "}],
    }
    p = _write_config(tmp_path, data)
    with pytest.raises(ConfigError, match="interests"):
        load_config(p)


def test_invalid_tolerance_raises(tmp_path: Path) -> None:
    data = {
        **_VALID_DATA,
        "senders": [{**_VALID_DATA["senders"][0], "tolerance": "yolo"}],
    }
    p = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(p)


def test_relative_paths_resolved_to_config_dir(tmp_path: Path) -> None:
    p = _write_config(tmp_path, _VALID_DATA)
    config = load_config(p)
    assert config.gmail.credentials_file == tmp_path / "credentials.json"
    assert config.gmail.token_file == tmp_path / "token.json"
    assert config.decision_log.path == tmp_path / "decisions.db"


def test_absolute_paths_unchanged(tmp_path: Path) -> None:
    data = {
        **_VALID_DATA,
        "gmail": {
            "credentials_file": "/absolute/credentials.json",
            "token_file": "/absolute/token.json",
        },
    }
    p = _write_config(tmp_path, data)
    config = load_config(p)
    assert config.gmail.credentials_file == Path("/absolute/credentials.json")


def test_tilde_expansion(tmp_path: Path) -> None:
    data = {
        **_VALID_DATA,
        "gmail": {
            "credentials_file": "~/secrets/credentials.json",
            "token_file": "~/secrets/token.json",
        },
    }
    p = _write_config(tmp_path, data)
    config = load_config(p)
    assert str(config.gmail.credentials_file).startswith("/")
    assert "~" not in str(config.gmail.credentials_file)


def test_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EM_PHI_DATA", "/mnt/data")
    data = {
        **_VALID_DATA,
        "gmail": {
            "credentials_file": "$EM_PHI_DATA/credentials.json",
            "token_file": "$EM_PHI_DATA/token.json",
        },
    }
    p = _write_config(tmp_path, data)
    config = load_config(p)
    assert config.gmail.credentials_file == Path("/mnt/data/credentials.json")
