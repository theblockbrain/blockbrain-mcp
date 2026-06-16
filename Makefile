.PHONY: help sync dev tunnel test lint fmt clean

help:  ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync:  ## Install / sync dependencies with uv.
	uv sync

dev:  ## Run the MCP server locally.
	uv run blockbrain-mcp

serve:  ## One-shot: start server + cloudflared tunnel + wire the public URL into the server.
	./scripts/dev-tunnel.sh

tunnel:  ## Just the tunnel (assumes server already running on MCP_PORT).
	@if command -v cloudflared >/dev/null 2>&1; then \
	    cloudflared tunnel --url http://localhost:$${MCP_PORT:-8080}; \
	elif command -v npx >/dev/null 2>&1; then \
	    npx --yes cloudflared tunnel --url http://localhost:$${MCP_PORT:-8080}; \
	else \
	    echo "✗ cloudflared not found. Run: brew install cloudflared"; exit 1; \
	fi

test:  ## Run pytest.
	uv run pytest

lint:  ## Ruff lint + format check.
	uv run ruff check
	uv run ruff format --check

fmt:  ## Ruff format + auto-fix.
	uv run ruff format
	uv run ruff check --fix

clean:  ## Remove caches & build artifacts.
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
