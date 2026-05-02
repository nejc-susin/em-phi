import importlib
import logging
from pathlib import Path

import click

from em_phi.classifiers.base import Classifier
from em_phi.config import AppConfig, ConfigError, load_config
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.providers.base import EmailProvider


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

    click.echo(f"Config:        {config_path.resolve()}")
    click.echo(f"email_provider: {config.email_provider.name}")
    click.echo(f"llm:           {config.llm.name}  (model={config.llm.model})")
    click.echo(f"Labels:        {config.labels.relevant} / {config.labels.irrelevant}")
    click.echo(f"Decision log:  {config.decision_log.path}")
    click.echo()

    click.echo(f"Senders ({len(config.senders)}):")
    for s in config.senders:
        emails = ", ".join(s.email)
        click.echo(f"  {emails:<35} \"{s.name}\"  [{s.tolerance}, {s.action}]")

    click.echo()
    click.echo("Paths:")
    ep = config.email_provider
    if ep.credentials_file:
        _report_path("credentials_file", ep.credentials_file, missing_hint="run `em-phi setup`")
    if ep.token_file:
        _report_path("token_file", ep.token_file, missing_hint="run `em-phi setup`")
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


@cli.command("run")
@click.option("--dry-run", is_flag=True, help="Classify emails but do not label, archive, or log.")
@click.option("--sender", default=None, help="Process only this sender email address.")
@click.pass_context
def run_cmd(ctx: click.Context, dry_run: bool, sender: str | None) -> None:
    """Process new emails from configured senders.

    Fetches unread messages, classifies each one with Claude, applies
    labels/archiving based on the verdict, and logs every decision.
    Use --dry-run to preview what would happen without touching Gmail.
    """
    from em_phi.processor import process_all

    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    _configure_logging(config)

    if dry_run:
        click.echo("[DRY RUN] No changes will be made to Gmail or the decision log.")
        click.echo()

    # Validate sender filter
    if sender:
        known = {e for s in config.senders for e in s.email}
        if sender not in known:
            raise click.ClickException(
                f"Sender '{sender}' not found in config.\nKnown senders: {', '.join(sorted(known))}"
            )

    provider = _build_provider(config)
    try:
        provider.authenticate()
    except RuntimeError as e:
        raise click.ClickException(str(e))

    try:
        classifier = _build_classifier(config)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    log = DecisionLog(config.decision_log.path)

    # Track current sender for section headers
    _current_sender: list[str] = []

    def on_email(email: Email, verdict: Verdict, action: str, is_dry: bool) -> None:
        verdict_tag = f"{verdict.verdict:<11} / {verdict.confidence:<6}"
        dry_tag = "[DRY RUN] " if is_dry else ""
        click.echo(f"  {dry_tag}[{verdict_tag}] {email.subject[:60]}  →  {action}")

    def on_error(context: str, exc: Exception) -> None:
        click.echo(f"  WARNING: error {context}: {exc}", err=True)

    senders_to_run = (
        [s for s in config.senders if sender in s.email] if sender else config.senders
    )

    total_processed = total_relevant = total_irrelevant = total_skipped = total_errors = 0

    for s in senders_to_run:
        click.echo(f"Processing {s.email} (\"{s.name}\")...")

        from em_phi.processor import _process_sender
        result = _process_sender(
            sender=s,
            config=config,
            provider=provider,
            classifier=classifier,
            log=log,
            dry_run=dry_run,
            on_email=on_email,
            on_error=on_error,
        )

        parts = [
            f"{result.processed} processed",
            f"{result.relevant} relevant",
            f"{result.irrelevant} irrelevant",
            f"{result.skipped} skipped",
        ]
        if result.errors:
            parts.append(f"{result.errors} errors")
        click.echo(f"  Done: {', '.join(parts)}")
        click.echo()

        total_processed += result.processed
        total_relevant += result.relevant
        total_irrelevant += result.irrelevant
        total_skipped += result.skipped
        total_errors += result.errors

    summary_parts = [
        f"{total_processed} processed",
        f"{total_relevant} relevant",
        f"{total_irrelevant} irrelevant",
        f"{total_skipped} skipped",
    ]
    if total_errors:
        summary_parts.append(f"{total_errors} errors")
    click.echo(f"Run complete: {', '.join(summary_parts)}")


@cli.command("debug")
@click.option("--sender", default=None, help="Inspect emails from this sender only.")
@click.option("--limit", default=1, show_default=True, help="Number of emails to inspect.")
@click.pass_context
def debug_cmd(ctx: click.Context, sender: str | None, limit: int) -> None:
    """Fetch emails and print the classifier prompt without calling the LLM.

    Useful for checking what Claude would see before a real run.
    Only works with the built-in Claude classifier.
    """
    from em_phi.debug import fetch_debug_info

    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    if config.llm.name != "claude":
        raise click.ClickException(
            f"debug only supports the built-in 'claude' classifier, got '{config.llm.name}'"
        )

    if sender:
        known = {e for s in config.senders for e in s.email}
        if sender not in known:
            raise click.ClickException(
                f"Sender '{sender}' not found in config.\nKnown senders: {', '.join(sorted(known))}"
            )

    provider = _build_provider(config)
    try:
        provider.authenticate()
    except RuntimeError as e:
        raise click.ClickException(str(e))

    try:
        infos = fetch_debug_info(config, provider, sender_filter=sender, limit=limit)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    if not infos:
        click.echo("No unread emails found for the specified sender(s).")
        return

    width = 72
    for i, info in enumerate(infos, 1):
        click.echo("=" * width)
        click.echo(f"  Email {i}/{len(infos)}  |  {info.email.message_id}")
        click.echo(f"  Sender:  {info.email.sender}")
        click.echo(f"  Subject: {info.email.subject}")
        click.echo(f"  Date:    {info.email.received_at.strftime('%Y-%m-%d %H:%M UTC')}")
        click.echo(f"  Body:    {len(info.email.body)} chars raw → {len(info.processed_email.body)} chars after preprocessing")
        click.echo("=" * width)
        click.echo()
        click.echo("--- SYSTEM PROMPT " + "-" * (width - 18))
        click.echo(info.system_prompt)
        click.echo()
        click.echo("--- USER MESSAGE " + "-" * (width - 17))
        click.echo(info.user_message)
        click.echo()


@cli.command("serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Start the web UI with optional in-process scheduler."""
    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    if not config.web:
        raise click.ClickException(
            "No 'web:' block found in config. Add one to use 'em-phi serve'.\n\n"
            "Example:\n  web:\n    host: 127.0.0.1\n    port: 8080\n    auth_token: your-secret-token"
        )

    import uvicorn
    from em_phi.web.app import create_app

    app = create_app(config, config_path)
    uvicorn.run(app, host=config.web.host, port=config.web.port)


def _build_provider(config: AppConfig) -> EmailProvider:
    """Instantiate the email provider named in config.email_provider.name.

    Built-in: "gmail"
    Custom:   add src/em_phi/providers/myprovider.py with create(config) -> EmailProvider
    """
    name = config.email_provider.name
    if name == "gmail":
        from em_phi.providers.gmail import GmailProvider
        ep = config.email_provider
        if not ep.credentials_file or not ep.token_file:
            raise click.ClickException(
                "email_provider 'gmail' requires credentials_file and token_file under email_provider:"
            )
        return GmailProvider(ep.credentials_file, ep.token_file, ep.fetch_label)
    try:
        module = importlib.import_module(f"em_phi.providers.{name}")
    except ImportError as e:
        raise click.ClickException(
            f"Unknown email_provider '{name}': cannot import em_phi.providers.{name}\n{e}"
        )
    if not hasattr(module, "create"):
        raise click.ClickException(
            f"em_phi.providers.{name} must define create(config: AppConfig) -> EmailProvider"
        )
    return module.create(config)


def _build_classifier(config: AppConfig) -> Classifier:
    """Instantiate the classifier named in config.llm.name.

    Built-in: "claude"
    Custom:   add src/em_phi/classifiers/myclassifier.py with create(config) -> Classifier
    """
    name = config.llm.name
    if name == "claude":
        from em_phi.classifiers.claude import ClaudeClassifier
        return ClaudeClassifier(config.llm)
    try:
        module = importlib.import_module(f"em_phi.classifiers.{name}")
    except ImportError as e:
        raise click.ClickException(
            f"Unknown llm '{name}': cannot import em_phi.classifiers.{name}\n{e}"
        )
    if not hasattr(module, "create"):
        raise click.ClickException(
            f"em_phi.classifiers.{name} must define create(config: AppConfig) -> Classifier"
        )
    return module.create(config)


def _configure_logging(config: AppConfig) -> None:
    log_cfg = config.logging
    level = getattr(logging, log_cfg.level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    handlers: list[logging.Handler] = [console]

    if log_cfg.file:
        fh = logging.FileHandler(str(log_cfg.file), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
        handlers.append(fh)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def _report_path(label: str, path: Path, *, missing_hint: str) -> None:
    if path.exists():
        click.echo(f"  {label:<20} {path}  [ok]")
    else:
        click.echo(f"  {label:<20} {path}  [not found — {missing_hint}]")
