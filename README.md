# em-phi

Self-hosted, AI-powered email filtering for Gmail newsletters.

em-phi connects to your Gmail account, reads incoming emails from senders you configure, passes each one to Claude with a per-rule interest profile, and routes it based on the verdict — label, archive, or leave in inbox. Everything is driven by a single YAML config file. No third-party service handling your inbox.

---

## Features

- **Per-rule interest profiles** — each newsletter rule gets its own natural-language description of what you want to read
- **Three-level tolerance** — `aggressive`, `balanced`, or `conservative` per rule
- **Safe by default** — labels only until you trust it; switch to archiving per-rule
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

Edit `config.yaml` with your rules and interest profiles. See [Config reference](#config-reference) below.

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
| `em-phi run` | Process new emails from all configured rules |
| `em-phi run --dry-run` | Classify and print verdicts without touching Gmail |
| `em-phi run --rule EMAIL` | Process only one rule |
| `em-phi check-config` | Validate config file and display parsed settings |
| `em-phi log` | Show recent decisions (last 20) |
| `em-phi log --rule EMAIL` | Filter log by rule email address |
| `em-phi log --days N` | Show decisions from the last N days |
| `em-phi log --limit N` | Show N entries (default: 20) |
| `em-phi debug` | Fetch the first unread email and print the exact prompt that would be sent to Claude |
| `em-phi debug --rule EMAIL` | Debug a specific rule |
| `em-phi debug --limit N` | Inspect the first N unread emails |

Global option: `--config PATH` (default: `config.yaml`, overridden by `EM_PHI_CONFIG` env var).

### debug command

`em-phi debug` is a prompt inspector. It authenticates with Gmail, fetches real unread emails, runs them through the same body preprocessing as a normal run (link stripping, 4000-character truncation), and prints exactly what would be sent to Claude — without making any LLM call or modifying your inbox.

```
========================================================================
  Email 1/1  |  18a3f2c9d4e1b
  Sender:  Python Weekly <editor@pyweekly.com>
  Subject: Issue #456 — Python 3.14 is here
  Date:    2026-05-01 08:00 UTC
  Body:    6241 chars raw → 3847 chars after preprocessing
========================================================================

--- SYSTEM PROMPT --------------------------------------------------
You are an email relevance classifier for a newsletter reader.

## Reader's interest profile for Python Weekly
I care about Python releases and security updates.
...

--- USER MESSAGE ---------------------------------------------------
## Email to classify
From: editor@pyweekly.com
Subject: Issue #456 — Python 3.14 is here
...
```

Use cases:
- **Tune your interest profile** — see exactly what Claude reads before adjusting `interests` in the config
- **Check preprocessing** — verify that link stripping and truncation are leaving the right content
- **Diagnose unexpected verdicts** — compare the prompt against Claude's reasoning in `em-phi log`

`ANTHROPIC_API_KEY` is not required. Only works with the built-in `claude` classifier.

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

rules:
  - email: newsletter@example.com      # exact From: address, comma-separated list, or domain
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

## Privacy

**What em-phi accesses**

em-phi only reads emails from senders you explicitly configure in rules. It does not scan your full inbox or access any other Gmail data. The Gmail OAuth scope requested is `gmail.modify` — the minimum needed to read emails and apply labels.

**What is sent to the Anthropic API**

For each matched email, the subject line and body text are sent to Claude for classification. The sender's address is not included. Your interest profile for that rule (from your config) is also sent as part of the prompt.

Body text is preprocessed before sending: links are replaced with a `<link>` placeholder and the text is truncated to 4000 characters.

Anthropic's standard API terms apply. If you want stronger guarantees, check whether [zero data retention](https://www.anthropic.com/privacy) is available on your plan.

**What is stored locally**

The decision log (`decisions.db`) records, for each processed email:

- Sender address, subject line, and timestamp
- The verdict (relevant / irrelevant), confidence, and Claude's one-sentence reason
- The action taken (label / archive)

Email body text is never written to the database or log files.

**What stays on your machine**

Everything else: your Gmail credentials (`credentials.json`, `token.json`), your config file with your interest profiles, and the decision database. No em-phi server is involved — the tool runs entirely on your own hardware and talks directly to the Gmail and Anthropic APIs.

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
