# syntax=docker/dockerfile:1

# Both stages start FROM the same Python image. The virtual environment built
# in stage 1 is tied to the interpreter it was created with (its site-packages
# path is `lib/python3.X`), so a builder and runtime on different Python minor
# versions produce an image that fails on start with
# `ModuleNotFoundError: No module named 'blockbrain_mcp'`. With one image
# reference used twice, Dependabot bumps both stages in the same PR.
# Keep the minor version in step with `.python-version` (what CI tests on).

# ─── Stage 1: build ──────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS builder

# uv is copied in as a static binary from its own pinned image instead of
# using a uv+Python image, so the Python version is decided only above.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

# Use the image's interpreter, never a uv-managed download; copy files out of
# the cache mount instead of hard-linking; precompile bytecode for startup.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

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
FROM python:3.14-slim-bookworm AS runtime

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
