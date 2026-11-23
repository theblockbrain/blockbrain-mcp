"""
`read_url` — fetch a URL and return its main content as Markdown.

Every fetch goes through `url_guard`, which enforces https, blocks internal
address ranges on each redirect hop, and caps the response size. Page content is
untrusted input that lands in the model's context, so it is also length-capped.

⭐ REPLACEABLE — swap for Firecrawl, Jina Reader, or your own extraction pipeline.
   For most text-heavy HTML pages, `trafilatura` does an excellent job with zero
   configuration. It also extracts some basic metadata.
"""

from __future__ import annotations

import structlog
import trafilatura
from fastmcp.tools import tool
from pydantic import BaseModel

from ..config import get_settings
from ..logging_setup import safe_url
from ..url_guard import fetch_document
from ._base import get_user_context

log = structlog.get_logger(__name__)

_UA = "blockbrain-mcp/0.1 (+https://github.com/theblockbrain/blockbrain-mcp)"
_TIMEOUT_SECONDS = 15.0


class PageContent(BaseModel):
    url: str
    title: str | None = None
    author: str | None = None
    published: str | None = None
    markdown: str
    char_count: int
    truncated: bool = False


@tool(
    title="Read URL",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def read_url(url: str) -> PageContent:
    """Download an https URL and return its main article content as clean Markdown.

    Args:
        url: The absolute https URL to fetch. Private and reserved network
            ranges are rejected.
    """
    user = get_user_context()
    settings = get_settings()
    log.info("read_url.request", user_id=user.user_id, url=safe_url(url))

    final_url, html = await fetch_document(
        url,
        max_bytes=settings.read_url_max_bytes,
        timeout=_TIMEOUT_SECONDS,
        user_agent=_UA,
        allowed_domains=settings.allowed_read_domains,
    )

    # trafilatura: dense, well-tuned. `output='markdown'` gives us clean headings/lists.
    markdown = (
        trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=False,
            favor_precision=True,
        )
        or ""
    )

    limit = settings.read_url_max_chars
    truncated = len(markdown) > limit
    if truncated:
        markdown = markdown[:limit]

    meta = trafilatura.extract_metadata(html)
    return PageContent(
        url=final_url,
        title=meta.title if meta else None,
        author=meta.author if meta else None,
        published=meta.date if meta else None,
        markdown=markdown,
        char_count=len(markdown),
        truncated=truncated,
    )
