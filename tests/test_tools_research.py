"""Tests for the `research_topic` composite tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from blockbrain_mcp.tools.research import research_topic
from blockbrain_mcp.tools.web_search import BRAVE_ENDPOINT


@pytest.mark.asyncio
async def test_research_topic_combines_search_and_read(mock_user_context) -> None:
    html = (
        "<html><body><article>"
        "<h1>Example</h1>"
        "<p>Enough text to trigger trafilatura precision extraction. "
        "A second sentence for good measure. And a third one to be sure.</p>"
        "<p>Another paragraph so the extractor keeps content rather than rejecting the page.</p>"
        "</article></body></html>"
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(BRAVE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {"title": "A", "url": "https://a.example.com/", "description": ""},
                            {"title": "B", "url": "https://b.example.com/", "description": ""},
                        ]
                    }
                },
            )
        )
        router.get("https://a.example.com/").mock(return_value=httpx.Response(200, text=html))
        router.get("https://b.example.com/").mock(return_value=httpx.Response(200, text=html))

        brief = await research_topic(topic="mcp", depth=2)

    assert brief.topic == "mcp"
    assert len(brief.sources) == 2
    assert "Enough text" in brief.combined_markdown


@pytest.mark.asyncio
async def test_research_topic_enforces_daily_quota(mock_user_context, monkeypatch) -> None:
    from blockbrain_mcp import config
    from blockbrain_mcp.tools import research

    monkeypatch.setenv("RATE_LIMIT_RESEARCH_PER_DAY", "0")
    config.get_settings.cache_clear()
    # also clear the module-level bucket so prior tests don't leak
    research._DAY_BUCKETS.clear()
    try:
        with pytest.raises(RuntimeError, match="quota"):
            await research_topic(topic="x", depth=1)
    finally:
        monkeypatch.setenv("RATE_LIMIT_RESEARCH_PER_DAY", "50")
        config.get_settings.cache_clear()
