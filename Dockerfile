# syntax=docker/dockerfile:1

# ─── Stage 1: build ──────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Leverage layer cache: copy lockfile + manifest first, install deps, THEN copy source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now copy source and install the project itself (no deps re-resolved).
# README.md is referenced by pyproject.toml's readme field — hatchling needs it.
COPY src/ ./src/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ─── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Non-root user
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid 1001 --no-create-home appuser

# Copy the virtual environment produced in the builder stage.
COPY --from=builder /app/.venv /app/.venv

# Copy source (needed for the package to be importable from the venv).
COPY --from=builder /app/src /app/src

# Make the venv's bin directory the first thing on PATH.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8080

# The entrypoint is the console-script registered in pyproject.toml
# [project.scripts]: blockbrain-mcp = "blockbrain_mcp.server:main"
ENTRYPOINT ["blockbrain-mcp"]
