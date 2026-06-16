"""Tests for the `whoami` tool."""

from __future__ import annotations

import pytest

from blockbrain_mcp.tools.whoami import whoami


@pytest.mark.asyncio
async def test_whoami_returns_user_identity(mock_user_context) -> None:
    result = await whoami()
    assert result.user_id == mock_user_context.user_id
    assert result.org_id == mock_user_context.org_id
    assert result.email == mock_user_context.email
    assert mock_user_context.email in result.message
