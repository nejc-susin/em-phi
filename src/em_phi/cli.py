import importlib
import logging
from pathlib import Path

import click

from em_phi.actions import ACTIONS_TAKEN
from em_phi.classifiers.base import Classifier
from em_phi.config import AppConfig, ConfigError, RuleConfig, is_weak_auth_token, load_config
from em_phi.decision_log import DecisionLog
from em_phi.models import Email, Verdict
from em_phi.processor import RuleResult, RunSummary, process_all
from em_phi.providers.base import EmailProvider
from em_phi.run_log import RunLog, RunRecord, RunRecorder


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

    click.echo(f"Rules ({len(config.rules)}):")
    for r in config.rules:
        emails = ", ".join(r.email)
        click.echo(f"  {emails:<35} \"{r.name}\"  [{r.tolerance}, {r.action}]")

    click.echo()
    click.echo("Paths:")
    ep = config.email_provider
    if ep.credentials_file:
        _report_path("credentials_file", ep.credentials_file, missing_hint="download from Google Cloud Console (docs/gmail-setup.md)")
    if ep.token_file:
        _report_path("token_file", ep.token_file, missing_hint="run oauth/authorize.py (docs/gmail-setup.md)")
    _report_path("decision_log", config.decision_log.path, missing_hint="created on first run")


@cli.command("log")
@click.option("--rule", default=None, help="Filter by rule name or sender address.")
@click.option("--days", default=None, type=click.IntRange(min=1), help="Limit to decisions from the last N days.")
@click.option("--verdict", default=None, type=click.Choice(["relevant", "irrelevant"]), help="Filter by verdict.")
@click.option("--action", "action_taken", default=None, type=click.Choice(list(ACTIONS_TAKEN)),
              help="Filter by action taken.")
@click.option("--search", default=None, help="Filter to entries whose subject or reason contains this text.")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1, max=500),
              help="Maximum number of entries to show.")
@click.option("--offset", default=0, show_default=True, type=click.IntRange(min=0),
              help="Number of matching entries to skip (for paging).")
@click.pass_context
def log_cmd(
    ctx: click.Context,
    rule: str | None,
    days: int | None,
    verdict: str | None,
    action_taken: str | None,
    search: str | None,
    limit: int,
    offset: int,
) -> None:
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
    log.backfill_rule_names(config.rules)

    # --rule accepts a rule name or a sender address; a name wins if both could apply.
    rule_name = rule if rule and any(r.name == rule for r in config.rules) else None
    rule_email = None if rule_name else rule
    entries = log.query(
        rule_email=rule_email, rule_name=rule_name, days=days, verdict=verdict, action=action_taken,
        search=search, limit=limit, offset=offset,
    )

    if not entries:
        click.echo("No entries found.")
        return

    # Header
    click.echo(f"{'Date (UTC)':<21}  {'Sender':<35}  {'Subject':<40}  {'Verdict':<20}  Action")
    click.echo("-" * 128)

    for e in entries:
        date = e.processed_at[:16] + " UTC"  # "2026-05-01 08:32 UTC"
        sender_col = e.sender[:35]
        subject_col = (e.subject[:37] + "...") if len(e.subject) > 40 else e.subject
        verdict_col = f"{e.verdict} ({e.confidence})"
        click.echo(f"{date:<21}  {sender_col:<35}  {subject_col:<40}  {verdict_col:<20}  {e.action_taken}")

    totals = log.count(
        rule_email=rule_email, rule_name=rule_name, days=days, verdict=verdict, action=action_taken,
        search=search,
    )
    click.echo()
    total = sum(totals.values())
    relevant = totals.get("relevant", 0)
    irrelevant = totals.get("irrelevant", 0)
    label = "Matching filter" if (rule or days or verdict or action_taken or search) else "Total in log"
    click.echo(f"{label}: {total}  ({relevant} relevant, {irrelevant} irrelevant)")


@cli.command("run")
@click.option("--dry-run", is_flag=True, help="Classify emails but do not label, archive, or log.")
@click.option("--rule", default=None, help="Process only this rule email address.")
@click.pass_context
def run_cmd(ctx: click.Context, dry_run: bool, rule: str | None) -> None:
    """Process new emails from configured rules.

    Fetches unread messages, classifies each one with Claude, applies
    labels/archiving based on the verdict, and logs every decision.
    Use --dry-run to preview what would happen without touching Gmail.
    """
    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    _configure_logging(config)

    if dry_run:
        click.echo("[DRY RUN] No changes will be made to Gmail or the decision log.")
        click.echo()

    # Validate rule filter
    if rule:
        known = {e for r in config.rules for e in r.email}
        if rule not in known:
            raise click.ClickException(
                f"Rule '{rule}' not found in config.\nKnown rules: {', '.join(sorted(known))}"
            )

    def on_email(email: Email, verdict: Verdict, action: str, is_dry: bool) -> None:
        verdict_tag = f"{verdict.verdict:<11} / {verdict.confidence:<6}"
        dry_tag = "[DRY RUN] " if is_dry else ""
        click.echo(f"  {dry_tag}[{verdict_tag}] {email.subject[:60]}  →  {action}")

    def on_error(context: str, exc: Exception) -> None:
        click.echo(f"  WARNING: error {context}: {exc}", err=True)

    def on_rule_start(r: RuleConfig) -> None:
        click.echo(f"Processing {r.email} (\"{r.name}\")...")

    def on_rule_done(r: RuleConfig, result: RuleResult) -> None:
        click.echo(f"  Done: {_format_counts(result)}")
        click.echo()

    # Recorded in the run history (`em-phi runs`, web Run page); a failure to
    # write it is logged and never stops the run.
    recorder = RunRecorder(
        config.decision_log.path, trigger="cli", dry_run=dry_run, rule_filter=rule, on_error=on_error
    )
    try:
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
        if not dry_run:
            log.backfill_rule_names(config.rules)

        summary = process_all(
            config=config,
            provider=provider,
            classifier=classifier,
            log=log,
            dry_run=dry_run,
            rule_filter=rule,
            on_email=on_email,
            on_error=recorder.errors,
            on_rule_start=on_rule_start,
            on_rule_done=on_rule_done,
        )
    except BaseException as exc:  # incl. Ctrl-C, so the run isn't left "running"
        recorder.fail(exc)
        raise
    recorder.finish(summary)

    click.echo(f"Run complete: {_format_counts(summary)}")


@cli.command("runs")
@click.option("--limit", default=10, show_default=True, type=click.IntRange(min=1, max=200),
              help="Maximum number of runs to show.")
@click.pass_context
def runs_cmd(ctx: click.Context, limit: int) -> None:
    """Show recent runs (web, scheduled and CLI) from the run history."""
    config_path: Path = ctx.obj["config_path"]

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e))

    db_path = config.decision_log.path
    runs = RunLog(db_path).recent(limit) if db_path.exists() else []
    if not runs:
        click.echo("No runs recorded yet.")
        return

    click.echo(
        f"{'Started (UTC)':<16}  {'Trigger':<9}  {'Dry':<3}  {'Rules':<24}  "
        f"{'Proc':>4}  {'Rel':>4}  {'Irr':>4}  {'Skip':>4}  {'Err':>4}  {'Status':<7}  Duration"
    )
    click.echo("-" * 106)
    for run in runs:
        rules = run.rule_filter or "all"
        rules = (rules[:21] + "...") if len(rules) > 24 else rules
        click.echo(
            f"{run.started_at[:16]:<16}  {run.trigger:<9}  {'dry' if run.dry_run else '':<3}  {rules:<24}  "
            f"{run.processed:>4}  {run.relevant:>4}  {run.irrelevant:>4}  {run.skipped:>4}  {run.errors:>4}  "
            f"{run.status:<7}  {run.duration_text}"
        )
        for line in _run_problem_lines(run):
            click.echo(f"    {line}")


_RUNS_ERRORS_SHOWN = 5


def _run_problem_lines(run: RunRecord) -> list[str]:
    lines: list[str] = []
    if run.failure:
        failure_lines = run.failure.splitlines() or [run.failure]
        lines.append(f"Failure: {failure_lines[0]}")
        lines.extend(f"         {extra}" for extra in failure_lines[1:])
    shown = run.error_messages[:_RUNS_ERRORS_SHOWN]
    lines.extend(f"Error: {message}" for message in shown)
    # error_messages is capped, so the error count can be the larger number.
    more = max(run.errors, len(run.error_messages)) - len(shown)
    if more > 0 and shown:
        lines.append(f"… and {more} more")
    return lines


def _format_counts(counts: RuleResult | RunSummary) -> str:
    parts = [
        f"{counts.processed} processed",
        f"{counts.relevant} relevant",
        f"{counts.irrelevant} irrelevant",
        f"{counts.skipped} skipped",
    ]
    if counts.errors:
        parts.append(f"{counts.errors} errors")
    return ", ".join(parts)


@cli.command("debug")
@click.option("--rule", default=None, help="Inspect emails from this rule only.")
@click.option("--limit", default=1, show_default=True, help="Number of emails to inspect.")
@click.pass_context
def debug_cmd(ctx: click.Context, rule: str | None, limit: int) -> None:
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

    if rule:
        known = {e for r in config.rules for e in r.email}
        if rule not in known:
            raise click.ClickException(
                f"Rule '{rule}' not found in config.\nKnown rules: {', '.join(sorted(known))}"
            )

    provider = _build_provider(config)
    try:
        provider.authenticate()
    except RuntimeError as e:
        raise click.ClickException(str(e))

    try:
        infos = fetch_debug_info(config, provider, rule_filter=rule, limit=limit)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    if not infos:
        click.echo("No unread emails found for the specified rule(s).")
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
            "Example:\n  web:\n    host: 0.0.0.0\n    port: 8080\n    auth_token: your-secret-token"
        )

    if is_weak_auth_token(config.web.auth_token):
        click.echo(
            "WARNING: web.auth_token is the default or too short — anyone who can reach this port "
            "can run em-phi and edit your config. Set a long random token.",
            err=True,
        )

    _configure_logging(config)

    import uvicorn

    from em_phi.web.app import create_app

    app = create_app(config, config_path)
    uvicorn.run(app, host=config.web.host, port=config.web.port)


def _build_provider(config: AppConfig) -> EmailProvider:
    name = config.email_provider.name
    if name == "gmail":
        from em_phi.providers.gmail import GmailProvider
        ep = config.email_provider
        if not ep.token_file:
            raise click.ClickException(
                "email_provider 'gmail' requires token_file under email_provider:"
            )
        return GmailProvider(ep.token_file, ep.fetch_label)
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
