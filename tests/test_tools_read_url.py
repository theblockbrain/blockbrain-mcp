"""Tests for the `read_url` tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from blockbrain_mcp.config import get_settings
from blockbrain_mcp.tools.read_url import read_url
from blockbrain_mcp.url_guard import ResponseTooLargeError, UnsafeUrlError

# Matches conftest.py's `_public_dns` fixture: every host resolves here, and the
# fetch guard pins the connection to this address rather than the hostname.
PUBLIC_IP = "93.184.216.34"


def _pinned(path: str) -> str:
    return f"https://{PUBLIC_IP}{path}"


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
        router.get(_pinned("/post"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, text=HTML_FIXTURE)
        )
        page = await read_url(url="https://example.com/post")

    assert page.url == "https://example.com/post"
    assert page.char_count > 0
    assert page.truncated is False
    assert "main article body" in page.markdown


@pytest.mark.asyncio
async def test_read_url_reports_the_final_url_after_a_redirect(mock_user_context) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/old"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(301, headers={"location": "https://example.com/new"})
        )
        router.get(_pinned("/new"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, text=HTML_FIXTURE)
        )
        page = await read_url(url="https://example.com/old")

    assert page.url == "https://example.com/new"


@pytest.mark.asyncio
async def test_read_url_refuses_the_cloud_metadata_endpoint(mock_user_context) -> None:
    with pytest.raises(UnsafeUrlError):
        await read_url(url="https://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_read_url_refuses_plain_http(mock_user_context) -> None:
    with pytest.raises(UnsafeUrlError):
        await read_url(url="http://example.com/post")


@pytest.mark.asyncio
async def test_read_url_refuses_a_redirect_into_the_private_network(mock_user_context) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/redirector"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(302, headers={"location": "https://10.0.0.5/admin"})
        )
        with pytest.raises(UnsafeUrlError):
            await read_url(url="https://example.com/redirector")


@pytest.mark.asyncio
async def test_read_url_rejects_a_body_over_the_byte_cap(
    mock_user_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READ_URL_MAX_BYTES", "500")
    get_settings.cache_clear()

    with respx.mock() as router:
        router.get(_pinned("/huge"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, text=HTML_FIXTURE * 200)
        )
        with pytest.raises(ResponseTooLargeError):
            await read_url(url="https://example.com/huge")


@pytest.mark.asyncio
async def test_read_url_truncates_markdown_at_the_character_cap(
    mock_user_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READ_URL_MAX_CHARS", "50")
    get_settings.cache_clear()

    with respx.mock() as router:
        router.get(_pinned("/post"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, text=HTML_FIXTURE)
        )
        page = await read_url(url="https://example.com/post")

    assert page.truncated is True
    assert page.char_count == 50


@pytest.mark.asyncio
async def test_read_url_honours_the_domain_allowlist(
    mock_user_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READ_URL_ALLOWED_DOMAINS", "example.com")
    get_settings.cache_clear()

    with pytest.raises(UnsafeUrlError, match="ALLOWED_DOMAINS"):
        await read_url(url="https://elsewhere.com/post")
