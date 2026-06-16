"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from blockbrain_mcp.context import UserContext

# Default test configuration: Entra single-tenant (the primary path).
os.environ.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("BRAVE_API_KEY", "test-brave-key")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Each test gets a fresh Settings instance so env mutations take effect."""
    from blockbrain_mcp import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture()
def sample_user() -> UserContext:
    return UserContext(
        user_id="user-123",
        org_id="org-456",
        email="alice@example.com",
        name="Alice",
    )


@pytest.fixture()
def mock_user_context(sample_user: UserContext) -> Iterator[UserContext]:
    """Patch `get_user_context` everywhere it is imported inside tool modules."""
    targets = [
        "blockbrain_mcp.tools._base.get_user_context",
        "blockbrain_mcp.tools.whoami.get_user_context",
        "blockbrain_mcp.tools.web_search.get_user_context",
        "blockbrain_mcp.tools.read_url.get_user_context",
        "blockbrain_mcp.tools.research.get_user_context",
    ]
    patches = [patch(t, return_value=sample_user) for t in targets]
    for p in patches:
        p.start()
    try:
        yield sample_user
    finally:
        for p in patches:
            p.stop()
