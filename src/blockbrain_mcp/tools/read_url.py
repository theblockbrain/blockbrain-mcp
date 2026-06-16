"""
`read_url` — fetch a URL and return its main content as Markdown.

⭐ REPLACEABLE — swap for Firecrawl, Jina Reader, or your own extraction pipeline.
   For most text-heavy HTML pages, `trafilatura` does an excellent job with zero
   configuration; it also extracts some basic metadata.
"""

from __future__ import annotations

import httpx
import structlog
import trafilatura
from pydantic import BaseModel

from ._base import get_user_context

log = structlog.get_logger(__name__)

_UA = "blockbrain-mcp/0.1 (+https://github.com/)"


class PageContent(BaseModel):
    url: str
    title: str | None = None
    author: str | None = None
    published: str | None = None
    markdown: str
    char_count: int


async def read_url(url: str) -> PageContent:
    """Download a URL and return its main article content as clean Markdown.

    Args:
        url: The absolute URL to fetch (http or https).
    """
    user = get_user_context()
    log.info("read_url.request", user_id=user.user_id, url=url)

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    # trafilatura: dense, well-tuned; `output='markdown'` gives us clean headings/lists.
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

    meta = trafilatura.extract_metadata(html)
    return PageContent(
        url=url,
        title=meta.title if meta else None,
        author=meta.author if meta else None,
        published=meta.date if meta else None,
        markdown=markdown,
        char_count=len(markdown),
    )
