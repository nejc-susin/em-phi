# CLAUDE.md

## Project overview

**em-phi** is a self-hosted, AI-powered Gmail newsletter filter. It fetches unread emails from configured senders, classifies each one with Claude Haiku using a per-sender interest profile, and labels or archives based on the verdict. Every decision is logged to SQLite.

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
uv run pytest              # all 34 tests, ~0.4s
uv run pytest tests/test_config.py -v   # one file
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
├── actions.py       # Applies verdicts to Gmail via the provider
├── decision_log.py  # SQLite decision log (also tracks processed message IDs)
├── providers/
│   ├── base.py      # EmailProvider Protocol (@runtime_checkable)
│   └── gmail.py     # GmailProvider — OAuth2, fetch, label, archive
└── classifiers/
    ├── base.py      # Classifier Protocol (@runtime_checkable)
    └── claude.py    # ClaudeClassifier — prompt construction, API call, JSON parsing
```

---

## Key design decisions

**Protocol-based modularity.** `providers/base.py` and `classifiers/base.py` define `@runtime_checkable` Protocols. The processor imports only from `base.py`. Adding a new provider or classifier means adding a new file and implementing the Protocol — no other changes needed.

**SQLite does double duty.** `decision_log.py` is both the human-readable audit log and the deduplication mechanism. `is_processed(message_id)` is an O(1) lookup used at the top of the processing loop to skip already-seen emails across runs.

**Prompt caching per sender.** In `classifiers/claude.py`, the system prompt contains the sender's interest profile and tolerance (which is constant per sender). It uses `cache_control: ephemeral`. When a run processes multiple emails from the same sender, only the first call is a cache miss.

**Path resolution.** All path fields in the config (credentials, token, db) go through `os.path.expandvars` + `os.path.expanduser`, then are resolved relative to the config file's directory if not absolute. This is handled in `config.py:AppConfig.resolve_relative_paths()`.

**Errors are non-fatal per email.** `processor._process_sender` catches exceptions from `get_message`, `classify`, and `apply_verdict` individually, calls `on_error`, and continues to the next message. Only `fetch_unread` failure aborts the whole sender.

**`ANTHROPIC_API_KEY` is never in config.** The key is read from the environment in `ClaudeClassifier.__init__`. The config only holds model name and max_tokens.

---

## Adding a new provider

1. Create `src/em_phi/providers/myprovider.py`
2. Implement the five methods from `providers/base.py:EmailProvider`
3. Define a top-level `create(config: AppConfig) -> EmailProvider` function
4. Set `provider: myprovider` in `config.yaml`

`cli.py:_build_provider` does `importlib.import_module("em_phi.providers.myprovider")` and calls `create(config)`. No other files need to change.

If your provider doesn't use Gmail credentials, omit the `gmail:` block from config — it's now optional. Only `provider: gmail` requires it.

## Adding a new classifier

1. Create `src/em_phi/classifiers/myclassifier.py`
2. Implement `classify(email: Email, sender: SenderConfig) -> Verdict` from `classifiers/base.py:Classifier`
3. Define a top-level `create(config: AppConfig) -> Classifier` function
4. Set `classifier: myclassifier` in `config.yaml`

`cli.py:_build_classifier` does `importlib.import_module("em_phi.classifiers.myclassifier")` and calls `create(config)`. No other files need to change.

---

## Config validation

`config.py:load_config(path)` is the single entry point. It:
1. Reads and parses YAML
2. Validates via `AppConfig.model_validate(data)` (Pydantic)
3. Resolves relative paths via `resolve_relative_paths(config_dir)`
4. Raises `ConfigError` (not `ValidationError`) with human-readable messages

All CLI commands that need the config call `load_config` and catch `ConfigError`, converting it to `click.ClickException`.

---

## Testing conventions

- Tests live in `tests/`, split by module: `test_config.py`, `test_classifier.py`, `test_processor.py`
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
