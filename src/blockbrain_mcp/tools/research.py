"""
`research_topic` — composite tool that combines `web_search` + `read_url`.

This is the "wow" moment of the template: one call produces a structured
research brief the LLM can reason over. It is also a useful example of how to
chain tools server-side (fewer LLM round-trips, coherent results).

Search results are attacker-influenced input: anyone who can rank a URL in the
search index gets it fetched. Every fetch therefore goes through `read_url`, which
applies the `url_guard` checks, and `READ_URL_ALLOWED_DOMAINS` narrows the reachable
hosts further when you know them up front.

⭐ REPLACEABLE / ADAPTABLE — customize the synthesis step to match your domain
   (e.g., summarize with an LLM, tighten the domain allow-list, emit citations in
   a specific format).

Per-user daily rate-limiting is implemented in-memory as a Blockbrain-friendly
example of applying `UserContext`. For production, move to Redis/DB.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import structlog
from fastmcp.tools import tool
from pydantic import BaseModel

from ..config import get_settings
from ..logging_setup import safe_url
from ._base import get_user_context
from .read_url import PageContent, read_url
from .web_search import web_search

log = structlog.get_logger(__name__)


class ResearchSource(BaseModel):
    title: str | None
    url: str
    excerpt: str


class ResearchBrief(BaseModel):
    topic: str
    sources: list[ResearchSource]
    combined_markdown: str


# --- tiny in-memory per-user day-counter (replace with Redis in prod) ---------
_DAY_BUCKETS: dict[tuple[str, int], int] = defaultdict(int)


def _check_and_increment_quota(user_id: str) -> None:
    day = int(time.time() // 86_400)
    key = (user_id, day)
    limit = get_settings().rate_limit_research_per_day
    if _DAY_BUCKETS[key] >= limit:
        raise RuntimeError(f"Daily research quota exceeded ({limit}/day) for user {user_id}.")
    _DAY_BUCKETS[key] += 1


@tool(
    title="Research a topic",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def research_topic(topic: str, depth: int = 3) -> ResearchBrief:
    """Search the web for a topic, read the top N results, and return a combined brief.

    Args:
        topic: What to research (e.g. "MCP OAuth 2.1 best practices").
        depth: How many top results to read in full (1-5).
    """
    user = get_user_context()
    _check_and_increment_quota(user.user_id)

    depth = max(1, min(depth, 5))
    log.info("research_topic.request", user_id=user.user_id, topic=topic, depth=depth)

    search = await web_search(query=topic, max_results=depth)

    pages: list[PageContent] = []
    # Fetch pages concurrently, skipping individual failures. A rejected URL is
    # an ordinary outcome here, not an error worth failing the whole brief for.
    results = await asyncio.gather(
        *(read_url(r.url) for r in search.results),
        return_exceptions=True,
    )
    for hit, result in zip(search.results, results, strict=True):
        if isinstance(result, PageContent):
            pages.append(result)
        else:
            log.warning(
                "research_topic.read_failed",
                url=safe_url(hit.url),
                error_type=type(result).__name__,
            )

    sources = [
        ResearchSource(
            title=p.title,
            url=p.url,
            excerpt=(p.markdown or "")[:500],
        )
        for p in pages
    ]

    combined = "\n\n---\n\n".join(
        f"# {p.title or p.url}\n\n_Source: {p.url}_\n\n{p.markdown}" for p in pages
    )

    return ResearchBrief(topic=topic, sources=sources, combined_markdown=combined)
