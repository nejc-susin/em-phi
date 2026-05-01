FROM python:3.12-slim

WORKDIR /app

# Tell uv to use the system Python instead of creating a virtual environment
ENV UV_SYSTEM_PYTHON=1

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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

# ENTRYPOINT ["em-phi"]
ENTRYPOINT ["uv", "run", "em-phi"]
CMD ["run"]
