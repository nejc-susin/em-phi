FROM python:3.12-slim

WORKDIR /app

# Install uv (pinned to the version that produced uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv

# 1. Install dependencies first (for better caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --no-cache

# 2. Copy source code
COPY README.md ./
COPY src/ ./src/

# 3. Install the project and set the PATH
# This creates the 'em-phi' executable inside /app/.venv/bin
RUN uv sync --frozen --no-dev --no-cache
ENV PATH="/app/.venv/bin:$PATH"

# Data directory configuration
# Mount your config.yaml, credentials.json, token.json, and decisions.db here
VOLUME ["/data"]

ENV EM_PHI_CONFIG=/data/config.yaml

# Port used by web mode (em-phi serve). Not needed for headless cron mode.
EXPOSE 8080

ENTRYPOINT ["em-phi"]
# Default: web mode. Use CMD ["run"] (or `command: ["run"]`) for the headless alternative.
CMD ["serve"]
