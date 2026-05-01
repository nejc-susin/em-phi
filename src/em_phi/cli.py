from pathlib import Path

import click

from em_phi.config import ConfigError, load_config


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


def _report_path(label: str, path: Path, *, missing_hint: str) -> None:
    if path.exists():
        click.echo(f"  {label:<20} {path}  [ok]")
    else:
        click.echo(f"  {label:<20} {path}  [not found — {missing_hint}]")
