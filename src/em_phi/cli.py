from pathlib import Path

import click

from em_phi.config import ConfigError, load_config
from em_phi.decision_log import DecisionLog


@click.group()
@click.option(
    "--config",
    envvar="EM_PHI_CONFIG",
    default="config.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to config file.",
)
@click.pass_context
def cli(ctx: click.Context, config: str) -> None:
    """em-phi — self-hosted AI email filtering for Gmail newsletters."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = Path(config)


@cli.command("check-config")
@click.pass_context
def check_config(ctx: click.Context) -> None:
    """Validate the config file and display parsed settings."""
    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    click.echo(f"Config:       {config_path.resolve()}")
    click.echo(f"Model:        {config.anthropic.model}  (max_tokens={config.anthropic.max_tokens})")
    click.echo(f"Labels:       {config.labels.relevant} / {config.labels.irrelevant}")
    click.echo(f"Decision log: {config.decision_log.path}")
    click.echo()

    click.echo(f"Senders ({len(config.senders)}):")
    for s in config.senders:
        click.echo(f"  {s.email:<35} \"{s.name}\"  [{s.tolerance}, {s.action}]")

    click.echo()
    click.echo("Paths:")
    _report_path("credentials_file", config.gmail.credentials_file, missing_hint="run `em-phi setup`")
    _report_path("token_file", config.gmail.token_file, missing_hint="run `em-phi setup`")
    _report_path("decision_log", config.decision_log.path, missing_hint="created on first run")


@cli.command("log")
@click.option("--sender", default=None, help="Filter by sender email address.")
@click.option("--days", default=None, type=int, help="Limit to decisions from the last N days.")
@click.option("--limit", default=20, show_default=True, help="Maximum number of entries to show.")
@click.pass_context
def log_cmd(ctx: click.Context, sender: str | None, days: int | None, limit: int) -> None:
    """Show recent decisions from the decision log."""
    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    db_path = config.decision_log.path
    if not db_path.exists():
        raise click.ClickException(f"Decision log not found: {db_path}\nRun `em-phi run` first.")

    log = DecisionLog(db_path)
    entries = log.query(sender=sender, days=days, limit=limit)

    if not entries:
        click.echo("No entries found.")
        return

    # Header
    click.echo(f"{'Date':<17}  {'Sender':<35}  {'Subject':<40}  {'Verdict':<16}  Action")
    click.echo("-" * 120)

    for e in entries:
        date = e.processed_at[:16]  # "2026-05-01 08:32"
        sender_col = e.sender[:35]
        subject_col = (e.subject[:37] + "...") if len(e.subject) > 40 else e.subject
        verdict_col = f"{e.verdict} ({e.confidence[:3]})"
        click.echo(f"{date:<17}  {sender_col:<35}  {subject_col:<40}  {verdict_col:<16}  {e.action_taken}")

    totals = log.count()
    click.echo()
    total = sum(totals.values())
    relevant = totals.get("relevant", 0)
    irrelevant = totals.get("irrelevant", 0)
    click.echo(f"Total in log: {total}  ({relevant} relevant, {irrelevant} irrelevant)")


def _report_path(label: str, path: Path, *, missing_hint: str) -> None:
    if path.exists():
        click.echo(f"  {label:<20} {path}  [ok]")
    else:
        click.echo(f"  {label:<20} {path}  [not found — {missing_hint}]")
