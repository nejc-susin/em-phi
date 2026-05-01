# em-phi

Self-hosted, AI-powered email filtering for Gmail newsletters.

em-phi connects to your Gmail account, reads incoming emails from senders you configure, passes each one to Claude with a per-sender interest profile, and routes it based on the verdict — label, archive, or leave in inbox. Everything is driven by a single YAML config file. No web UI, no third-party service handling your inbox.

---

## Features

- **Per-sender interest profiles** — each newsletter gets its own natural-language description of what you want to read
- **Three-level tolerance** — `aggressive`, `balanced`, or `conservative` per sender
- **Safe by default** — labels only until you trust it; switch to archiving per-sender
- **Full decision log** — every verdict written to SQLite for review and tuning
- **Dry-run mode** — preview what would happen without touching Gmail
- **Docker-friendly** — single volume mount, no browser required after initial setup
- **Modular** — swap Claude for another model, or Gmail for another provider, by implementing a Protocol

---

## Quick start

### 1. Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yourname/em-phi.git
cd em-phi
uv sync
```

### 2. Set up Gmail API credentials

Follow [docs/gmail-setup.md](docs/gmail-setup.md) to create a Google Cloud project, enable the Gmail API, and download `credentials.json`.

### 3. Create your config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your senders and interest profiles. See [Config reference](#config-reference) below.

### 4. Generate token.json

Follow [docs/gmail-setup.md](docs/gmail-setup.md) step 5 to run the one-time authorization script. It opens a browser for the Google OAuth2 consent screen and saves a refresh token to `token.json`.

### 5. Preview (dry run)

```bash
em-phi run --dry-run
```

### 6. Run for real

```bash
em-phi run
```

Then inspect the results:

```bash
em-phi log
```

---

## CLI reference

| Command | Description |
|---|---|
| `em-phi run` | Process new emails from all configured senders |
| `em-phi run --dry-run` | Classify and print verdicts without touching Gmail |
| `em-phi run --sender EMAIL` | Process only one sender |
| `em-phi check-config` | Validate config file and display parsed settings |
| `em-phi log` | Show recent decisions (last 20) |
| `em-phi log --sender EMAIL` | Filter log by sender |
| `em-phi log --days N` | Show decisions from the last N days |
| `em-phi log --limit N` | Show N entries (default: 20) |

Global option: `--config PATH` (default: `config.yaml`, overridden by `EM_PHI_CONFIG` env var).

---

## Config reference

```yaml
gmail:
  credentials_file: credentials.json   # path to OAuth2 client credentials
  token_file: token.json               # path to saved refresh token

anthropic:
  model: claude-haiku-4-5-20251001     # Claude model to use
  max_tokens: 256                      # max tokens in Claude's response

labels:
  relevant: "EmPhi/Relevant"           # Gmail label for relevant emails
  irrelevant: "EmPhi/Irrelevant"       # Gmail label for irrelevant emails

decision_log:
  path: decisions.db                   # SQLite database path

senders:
  - email: newsletter@example.com      # exact From: address to match
    name: "Example Newsletter"         # human-readable name (used in prompt)
    interests: |
      What you want to read from this sender.
      What you don't want. Be specific.
    tolerance: balanced                # aggressive | balanced | conservative
    action: label                      # label | archive
```

**Path fields** (`credentials_file`, `token_file`, `decision_log.path`) support:
- Relative paths — resolved relative to the config file's directory
- Absolute paths — `/data/credentials.json`
- Home directory — `~/secrets/credentials.json`
- Environment variables — `$EM_PHI_DATA/credentials.json`

**Tolerance levels:**
- `aggressive` — archive anything not clearly relevant; when in doubt, archive
- `balanced` — keep somewhat relevant emails; archive only clearly irrelevant ones
- `conservative` — keep anything even slightly relevant; archive only obvious misses

**Action** controls what happens to *irrelevant* emails. Relevant emails are always labelled and kept in inbox. Start with `label` for a few days to build confidence, then switch to `archive`.

---

## Scheduling

Run em-phi on a schedule to keep your inbox filtered automatically. See:

- [docs/deployment.md](docs/deployment.md) — cron and systemd timer
- [docs/docker.md](docs/docker.md) — Docker with host cron

---

## Extending em-phi

### Add a new email provider

Create a file in `src/em_phi/providers/` that implements the `EmailProvider` protocol defined in `providers/base.py`. The processor uses only the protocol interface.

### Add a new classifier

Create a file in `src/em_phi/classifiers/` that implements the `Classifier` protocol defined in `classifiers/base.py`.

---

## Running tests

```bash
uv run pytest
```

---

## License

MIT
