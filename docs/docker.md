# Docker

em-phi ships with a `Dockerfile` and `docker-compose.yaml`. All persistent state (config, credentials, token, decision log) lives in a single volume mount at `/data` inside the container.

---

## The OAuth2 constraint

Gmail OAuth2 requires a browser for the initial authorization. You must run `em-phi setup` on a machine with a browser **before** using Docker. After that, em-phi uses the saved refresh token silently and Docker works headlessly.

---

## Setup (one time)

### 1. Install on your local machine

```bash
git clone https://github.com/yourname/em-phi.git
cd em-phi
uv sync
```

### 2. Create your data directory and config

```bash
mkdir data
cp config.example.yaml data/config.yaml
```

Edit `data/config.yaml`. Set paths relative to `/data/`:

```yaml
gmail:
  credentials_file: /data/credentials.json
  token_file: /data/token.json

decision_log:
  path: /data/decisions.db
```

### 3. Copy your credentials file

Download `credentials.json` from Google Cloud Console (see [gmail-setup.md](gmail-setup.md)) and place it at `data/credentials.json`.

### 4. Run the OAuth2 setup flow on the host

```bash
export ANTHROPIC_API_KEY=sk-ant-...
em-phi --config data/config.yaml setup
```

This opens a browser, completes the OAuth2 flow, and writes `data/token.json`. You only need to do this once (or after revoking access).

---

## Running with Docker

### Build

```bash
docker build -t em-phi .
```

### Set your API key

Create a `.env` file (never commit this):

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

### Dry run to verify

```bash
docker compose run --rm em-phi run --dry-run
```

### Process emails

```bash
docker compose run --rm em-phi run
```

### View the decision log

```bash
docker compose run --rm em-phi log
```

---

## Scheduling with host cron

The recommended approach is to trigger `docker compose run` from a host cron job. The container starts, processes emails, and exits — no always-on container needed.

```bash
crontab -e
```

```cron
*/30 * * * * cd /path/to/em-phi && docker compose run --rm em-phi run >> /var/log/em-phi.log 2>&1
```

Make sure your `.env` file is in the same directory as `docker-compose.yaml` (Docker Compose picks it up automatically).

---

## Directory layout

```
em-phi/
├── docker-compose.yaml
├── .env                    # ANTHROPIC_API_KEY — gitignored, never commit
└── data/
    ├── config.yaml         # your config — gitignored
    ├── credentials.json    # from Google Cloud Console — gitignored
    ├── token.json          # from `em-phi setup` — gitignored
    └── decisions.db        # auto-created by em-phi — gitignored
```

All files under `data/` are bind-mounted into the container at `/data`.

---

## Updating

```bash
git pull
docker build -t em-phi .
```

The `data/` directory and all your state are untouched.

---

## Troubleshooting

**"Token not found"** — you need to run `em-phi setup` on the host first to generate `data/token.json`.

**"ANTHROPIC_API_KEY environment variable is not set"** — make sure `.env` exists in the project root with `ANTHROPIC_API_KEY=sk-ant-...`.

**Permission errors on `data/`** — the container runs as root by default. If your host files are owned by a non-root user, add `user: "${UID}:${GID}"` to the service in `docker-compose.yaml`.
