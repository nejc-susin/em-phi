from __future__ import annotations

from em_phi.config import AppConfig


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
