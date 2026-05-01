FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache -e "."

# Copy source
COPY src/ ./src/

# Data directory for config, credentials, and decision log
# Mount your config.yaml, credentials.json, token.json, and decisions.db here
VOLUME ["/data"]

ENV EM_PHI_CONFIG=/data/config.yaml

ENTRYPOINT ["em-phi"]
CMD ["run"]
