"""Tests for the `read_url` tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from blockbrain_mcp.tools.read_url import read_url

HTML_FIXTURE = """
<!doctype html>
<html>
<head><title>Test Page</title><meta name="author" content="Alice"></head>
<body>
<article>
  <h1>Test Page</h1>
  <p>This is the main article body with enough text to satisfy trafilatura's precision mode.
     It keeps going to make sure the extractor returns something useful rather than None.
     Trafilatura favors precision over recall when `favor_precision=True`, which means we
     need a handful of sentences before it keeps anything at all.</p>
  <p>A second paragraph, also non-trivial in length, helps the extractor decide this is real
     content and not a navigation block or a footer.</p>
</article>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_read_url_extracts_markdown(mock_user_context) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/post").mock(
            return_value=httpx.Response(200, text=HTML_FIXTURE)
        )
        page = await read_url(url="https://example.com/post")

    assert page.url == "https://example.com/post"
    assert page.char_count > 0
    assert "main article body" in page.markdown
