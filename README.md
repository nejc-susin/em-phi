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
git clone https://github.com/nejc-susin/em-phi.git
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

Follow [docs/gmail-setup.md](docs/gmail-setup.md) step 5 to run the one-time authorization script (`oauth/authorize.py`). It opens a browser for the Google OAuth2 consent screen and saves a refresh token to `token.json`.

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

To run em-phi always-on with a dashboard and built-in scheduler, see [Web UI](#web-ui) and [docs/docker.md](docs/docker.md).

---

## CLI reference

| Command | Description |
|---|---|
| `em-phi run` | Process new emails from all configured rules |
| `em-phi run --dry-run` | Classify and print verdicts without touching Gmail |
| `em-phi run --rule EMAIL` | Process only one rule |
| `em-phi check-config` | Validate config file and display parsed settings |
| `em-phi log` | Show recent decisions (last 20) |
| `em-phi log --rule NAME\|EMAIL` | Filter log by rule name or sender address |
| `em-phi log --days N` | Show decisions from the last N days |
| `em-phi log --limit N` | Show N entries (default: 20) |
| `em-phi log --offset N` | Skip the first N matching entries (for paging) |
| `em-phi log --verdict relevant\|irrelevant` | Filter log by verdict |
| `em-phi log --action label\|archive\|inbox` | Filter log by action taken |
| `em-phi log --search TEXT` | Show entries whose subject or reason contains TEXT |
| `em-phi debug` | Fetch the first unread email and print the exact prompt that would be sent to Claude |
| `em-phi debug --rule EMAIL` | Debug a specific rule |
| `em-phi debug --limit N` | Inspect the first N unread emails |
| `em-phi runs` | Show recent runs (web, scheduled and CLI) with counts, status and any errors (last 10; `--limit N` for more) |
| `em-phi serve` | Start the web UI (dashboard, log viewer, config editor) with the built-in scheduler; needs a `web:` block in the config |

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

## Web UI

`em-phi serve` starts a FastAPI dashboard on `web.host:web.port`. Add a `web:` block to the config first (see [Config reference](#config-reference)). The UI is protected by `web.auth_token`: sign in with the token, and change the default `change-me`.

Pages:

- **Run** — run now, dry run, or run a single rule, with verdicts streamed live as emails are classified; a status panel (Gmail token, Claude API key, schedule, last run) and the recent run history, including errors from scheduled runs
- **Log** — the decision log, filterable by rule, days, verdict, action and text, with paging
- **Debug** — prompt inspector, the web version of `em-phi debug`
- **Config** — edit rules, schedule and settings. Saving rewrites `config.yaml` and drops any comments in it

**Built-in scheduler.** Set `schedule.enabled: true` and either `interval_hours` or a `cron` expression (cron wins if both are set). The scheduler only runs in serve mode; `em-phi run` ignores it.

`serve` is the default mode of the Docker image. See [docs/docker.md](docs/docker.md).

---

## Recommended Gmail setup

This is the label-driven workflow the author uses.

1. In Gmail, create a filter per newsletter sender, or one filter with `from:(a@x.com OR b@y.com)`, that applies a label such as `EmPhi`. Go to Settings → Filters → Create filter → **Apply the label**.
2. Set `email_provider.fetch_label: EmPhi`. em-phi then only looks at mail carrying that label. The Gmail query becomes the rule's `from:` clause + `is:unread` + `label:EmPhi`. Rules still need to match the sender; the label just narrows what is fetched.
3. Optional "gatekeeper" variant: also tick **Skip the Inbox (Archive it)** in the Gmail filter and use `action: inbox` on the rule. Newsletters never hit the inbox directly. em-phi moves only the relevant ones back in and leaves the rest archived with the irrelevant label.
4. Start with `action: label` and check the Log for a few days before archiving.
5. If you often read newsletters on your phone before em-phi runs, set `unread_only: false` (with `lookback_days`) so they are still labelled. Read emails are never moved back into the inbox by `action: inbox`; they only get the label.

```yaml
email_provider:
  name: gmail
  credentials_file: credentials.json
  token_file: token.json
  fetch_label: EmPhi

rules:
  - email: newsletter@example.com
    name: "Example Newsletter"
    interests: |
      What you want to read from this sender.
    tolerance: balanced
    action: inbox
```

---

## Config reference

```yaml
email_provider:
  name: gmail
  credentials_file: credentials.json   # path to OAuth2 client credentials
  token_file: token.json               # path to saved refresh token
  # fetch_label: "ToFilter"            # optional: only process emails carrying this Gmail label
  # unread_only: false                 # optional: also process emails you already read (default: true)
  # lookback_days: 3                   # with unread_only: false, how far back to look, 1-30 (default: 3)
  # account: "0"                       # Gmail web account for log links: index (0, 1, ...) or email address

llm:
  name: claude
  model: claude-haiku-5-5              # Claude model to use
  max_tokens: 1024                     # max tokens in Claude's response

labels:
  relevant: "EmPhi/Relevant"           # Gmail label for relevant emails
  irrelevant: "EmPhi/Irrelevant"       # Gmail label for irrelevant emails

decision_log:
  path: decisions.db                   # SQLite database path

# logging:
#   level: WARNING                     # DEBUG | INFO | WARNING | ERROR
#   file: null                         # optional log file path

# schedule:                            # web mode only (em-phi serve)
#   enabled: false
#   interval_hours: 6                  # >= 1
#   # cron: "0 */6 * * *"              # takes precedence over interval_hours

# web:                                 # required for em-phi serve
#   host: 127.0.0.1                    # use 0.0.0.0 inside Docker
#   port: 8080
#   auth_token: change-me

rules:
  - email: newsletter@example.com      # exact From: address, comma-separated list, or domain
    name: "Example Newsletter"         # human-readable name (used in prompt)
    interests: |
      What you want to read from this sender.
      What you don't want. Be specific.
    tolerance: balanced                # aggressive | balanced | conservative
    action: label                      # label | archive | inbox
```

Unknown keys are rejected, so typos fail `em-phi check-config` instead of being silently ignored. Configs using the old top-level `gmail:` / `anthropic:` blocks must be renamed to `email_provider:` / `llm:`.

**Path fields** (`credentials_file`, `token_file`, `decision_log.path`) support:
- Relative paths — resolved relative to the config file's directory
- Absolute paths — `/data/credentials.json`
- Home directory — `~/secrets/credentials.json`
- Environment variables — `$EM_PHI_DATA/credentials.json`

**Tolerance levels:**
- `aggressive` — archive anything not clearly relevant; when in doubt, archive
- `balanced` — keep somewhat relevant emails; archive only clearly irrelevant ones
- `conservative` — keep anything even slightly relevant; archive only obvious misses

**Action** controls what happens after classification:
- `label` — apply the relevant/irrelevant label only; nothing is moved
- `archive` — label both; archive irrelevant emails (relevant ones stay where they are)
- `inbox` — label both; move relevant emails to the inbox and archive irrelevant ones

Start with `label` for a few days to build confidence, then switch to `archive` or `inbox`.

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
- The action taken (label / archive / inbox)

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
uv run python -m pytest
```

---

## License

MIT
