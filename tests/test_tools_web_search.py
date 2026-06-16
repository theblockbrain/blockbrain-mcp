"""Tests for the `web_search` tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from blockbrain_mcp.tools.web_search import BRAVE_ENDPOINT, web_search


@pytest.mark.asyncio
async def test_web_search_parses_brave_response(mock_user_context) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(BRAVE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "MCP OAuth 2.1 Explained",
                                "url": "https://example.com/a",
                                "description": "A guide to MCP OAuth 2.1.",
                            },
                            {
                                "title": "Resource Indicators",
                                "url": "https://example.com/b",
                                "description": "RFC 8707.",
                            },
                        ]
                    }
                },
            )
        )
        result = await web_search(query="mcp oauth", max_results=2)

    assert result.query == "mcp oauth"
    assert len(result.results) == 2
    assert result.results[0].url == "https://example.com/a"
    assert result.results[1].snippet == "RFC 8707."


@pytest.mark.asyncio
async def test_web_search_raises_without_api_key(mock_user_context, monkeypatch) -> None:
    from blockbrain_mcp import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("BRAVE_API_KEY", "")
    try:
        with pytest.raises(RuntimeError, match="BRAVE_API_KEY"):
            await web_search(query="x")
    finally:
        monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
        config.get_settings.cache_clear()
