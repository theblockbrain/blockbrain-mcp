"""
`web_search` — Brave Search API wrapper.

⭐ REPLACEABLE — a typical customer tool. Replace with your own search backend
   (Tavily, Perplexity, internal Elasticsearch, etc.) by changing the body of
   `web_search()` below. The signature is what MCP clients see, so keep it
   stable if you want to avoid re-indexing tool schemas upstream.

Free tier: https://brave.com/search/api/  (2000 queries / month).
"""

from __future__ import annotations

import httpx
import structlog
from pydantic import BaseModel, Field

from ..config import get_settings
from ._base import get_user_context

log = structlog.get_logger(__name__)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = Field(default="", description="Short description from the search engine.")


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]


async def web_search(query: str, max_results: int = 5) -> SearchResponse:
    """Search the web via Brave Search. Returns a list of result cards.

    Args:
        query: Natural-language search query.
        max_results: How many results to return (1-20).
    """
    settings = get_settings()
    if not settings.brave_api_key:
        raise RuntimeError("BRAVE_API_KEY is not configured. See .env.example.")

    user = get_user_context()
    log.info("web_search.request", user_id=user.user_id, query=query, max_results=max_results)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            BRAVE_ENDPOINT,
            params={"q": query, "count": max(1, min(max_results, 20))},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    hits = (data.get("web") or {}).get("results") or []
    results = [
        SearchResult(
            title=hit.get("title", ""),
            url=hit.get("url", ""),
            snippet=hit.get("description", ""),
        )
        for hit in hits[:max_results]
    ]
    return SearchResponse(query=query, results=results)
