# Build from the repository root.
FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM python:3.13-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 LOG_FORMAT=json
COPY services/agent/pyproject.toml services/agent/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY services/agent/README.md ./README.md
COPY services/agent/src ./src
RUN uv sync --locked --no-dev \
    && useradd --system --uid 10001 kiki
USER kiki
ENV PATH="/app/.venv/bin:$PATH" PORT=8081
CMD ["python", "-m", "kiki_agent_host"]
