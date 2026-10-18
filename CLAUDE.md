# CLAUDE.md

## Deploy to Docker Hub

Image: `nejcsusin/em-phi` — multi-platform (linux/amd64 + linux/arm64).

```bash
cd /Users/nejcsusin/git/em-phi
docker buildx build --platform linux/amd64,linux/arm64 -t nejcsusin/em-phi:latest --push .
```

The buildx builder (`em-phi-builder`) was created once and persists. If it's ever missing, recreate it with:
```bash
docker buildx create --use --name em-phi-builder
```

On the server, pull and restart:
```bash
docker compose pull && docker compose up -d
```

---

## Project overview

**em-phi** is a self-hosted, AI-powered Gmail newsletter filter. It fetches unread emails from configured rules, classifies each one with Claude Haiku using a per-rule interest profile, and labels or archives based on the verdict. Every decision is logged to SQLite.

Python package: `em_phi` — CLI command: `em-phi` — entry point: `src/em_phi/cli.py`.

---

## Dev environment

```bash
uv sync                    # install deps including dev extras
source .venv/bin/activate  # activate the venv
em-phi --help              # verify CLI works
```

Run tests:

```bash
uv run python -m pytest    # all 114 tests, ~2s
uv run python -m pytest tests/test_config.py -v   # one file
```

No live Gmail or Anthropic credentials are needed for tests — all external calls are mocked.

---

## Package layout

```
src/em_phi/
├── cli.py           # Click CLI — all user-facing commands
├── config.py        # YAML loading + Pydantic validation + path expansion
├── models.py        # Email, Verdict dataclasses
├── processor.py     # Core loop: fetch → skip → classify → act → log
├── debug.py         # Prompt inspector behind `em-phi debug` and the web debug page
├── actions.py       # Applies verdicts to Gmail via the provider
├── decision_log.py  # SQLite decision log (also tracks processed message IDs)
├── providers/
│   ├── base.py      # EmailProvider Protocol (@runtime_checkable)
│   └── gmail.py     # GmailProvider — OAuth2, fetch, label, archive
├── classifiers/
│   ├── base.py      # Classifier Protocol (@runtime_checkable)
│   └── claude.py    # ClaudeClassifier — prompt construction, API call, JSON parsing
└── web/             # FastAPI UI behind `em-phi serve`
    ├── app.py       # App factory, token auth, cross-site request blocking
    ├── state.py     # Shared state (live config, run lock)
    ├── scheduler.py # Built-in scheduler (interval or cron) for web mode
    ├── routes/
    │   ├── config.py   # Config editor (validate, then atomic write)
    │   ├── debug.py    # Prompt inspector page
    │   ├── log.py      # Decision log viewer
    │   ├── run.py      # Manual run controls
    │   └── _helpers.py # Shared route helpers
    ├── templates/   # Jinja2 templates
    └── static/
        └── tailwind.js  # Vendored Tailwind CSS script
```

---

## Key design decisions

**Protocol-based modularity.** `providers/base.py` and `classifiers/base.py` define `@runtime_checkable` Protocols. The processor imports only from `base.py`. Adding a new provider or classifier means adding a new file and implementing the Protocol — no other changes needed.

**SQLite does double duty.** `decision_log.py` is both the human-readable audit log and the deduplication mechanism. `is_processed(message_id)` is an O(1) lookup used at the top of the processing loop to skip already-seen emails across runs.

**Prompt caching per rule.** In `classifiers/claude.py`, the system prompt contains the rule's interest profile and tolerance (constant per rule) and is marked `cache_control: ephemeral`. Prompts shorter than the model's minimum cacheable length (512 tokens for Claude Haiku 5.5) silently don't cache, which is the case for most rules' short interest profiles. It's harmless, but not a guaranteed saving.

**Path resolution.** All path fields in the config (credentials, token, db) go through `os.path.expandvars` + `os.path.expanduser`, then are resolved relative to the config file's directory if not absolute. This is handled in `config.py:AppConfig.resolve_relative_paths()`.

**Errors are non-fatal per email.** `processor._process_rule` catches exceptions from `get_message`, `classify`, and `apply_verdict` individually, plus the decision-log write, calls `on_error`, and continues to the next message. Only `fetch_unread` failure aborts the whole rule.

**`ANTHROPIC_API_KEY` is never in config.** The key is read from the environment in `ClaudeClassifier.__init__`. The `llm:` block only holds model name and max_tokens.

---

## Adding a new email provider

1. Create `src/em_phi/providers/myprovider.py`
2. Implement the five methods from `providers/base.py:EmailProvider`
3. Define a top-level `create(config: AppConfig) -> EmailProvider` function
4. In `config.yaml`, set `email_provider.name: myprovider` and put any provider-specific fields under `email_provider:` — they're preserved in `config.email_provider.model_extra`

`cli.py:_build_provider` dynamically imports `em_phi.providers.myprovider` and calls `create(config)`. No other files change.

## Adding a new LLM classifier

1. Create `src/em_phi/classifiers/myclassifier.py`
2. Implement `classify(email: Email, rule: RuleConfig) -> Verdict` from `classifiers/base.py:Classifier`
3. Define a top-level `create(config: AppConfig) -> Classifier` function
4. In `config.yaml`, set `llm.name: myclassifier` and put any LLM-specific fields under `llm:` — they are preserved in `config.llm.model_extra`

`cli.py:_build_classifier` dynamically imports `em_phi.classifiers.myclassifier` and calls `create(config)`. No other files change.

---

## Config validation

`config.py:load_config(path)` is the single entry point. It:
1. Reads and parses YAML
2. Validates via `AppConfig.model_validate(data)` (Pydantic)
3. Resolves relative paths via `resolve_relative_paths(config_dir)`
4. Raises `ConfigError` (not `ValidationError`) with human-readable messages

All CLI commands that need the config call `load_config` and catch `ConfigError`, converting it to `click.ClickException`.

Unknown config keys are rejected (only `email_provider` and `llm` allow extra fields). The web config editor validates the whole config before atomically replacing `config.yaml`, and never mutates the live config on failure.

---

## Testing conventions

- Tests live in `tests/`, split by module: `test_classifier.py`, `test_cli.py`, `test_config.py`, `test_decision_log.py`, `test_gmail_body.py`, `test_gmail_provider.py`, `test_processor.py`, `test_web_auth.py`, `test_web_config.py`, `test_web_debug.py`, `test_web_log.py`, `test_web_run.py`, `test_web_static_assets.py`
- Shared fixtures in `tests/conftest.py` — use these rather than building config/email objects inline
- No live API calls — Gmail and Anthropic are mocked with `unittest.mock.MagicMock`
- `pytest-mock` is available but the tests mostly use `unittest.mock` directly
- `tmp_path` (pytest built-in) provides isolated temp directories; `tmp_db` fixture wraps it for SQLite

---

## What to avoid

- Do not add a `config.yaml` to the repo — it's gitignored. `config.example.yaml` is the template.
- Do not commit `credentials.json`, `token.json`, or `*.db` — all gitignored.
- Do not put `ANTHROPIC_API_KEY` in config or code — environment only.
- Do not add pagination to `fetch_unread` without also adding a `--backfill` flag — the current 100-message limit is intentional for the cron-run use case.
- Do not use `google.auth` `ServiceAccountCredentials` — em-phi uses user OAuth2 (`InstalledAppFlow`), not service accounts.
