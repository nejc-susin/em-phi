from __future__ import annotations

from em_phi.config import AppConfig
from em_phi.providers.gmail import message_url


def parse_int(raw: str | None) -> int | None:
    """Parse a query-string int, treating blank/invalid input as absent rather than erroring."""
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def rule_options(config: AppConfig) -> list[tuple[str, str]]:
    """(email address, rule name) pairs for the rule filter dropdowns, sorted by address."""
    return sorted(
        {(e, r.name) for r in config.rules for e in r.email},
        key=lambda pair: pair[0],
    )


def rule_name_options(config: AppConfig) -> list[tuple[str, str]]:
    """(rule name, comma-joined addresses) pairs in config order, one per distinct name."""
    options: dict[str, str] = {}
    for r in config.rules:
        options.setdefault(r.name, ", ".join(r.email))
    return list(options.items())


def message_link(config: AppConfig, message_id: str) -> str | None:
    """Gmail URL for a message, or None when the email provider isn't Gmail."""
    ep = config.email_provider
    if ep.name != "gmail":
        return None
    return message_url(message_id, ep.account)
