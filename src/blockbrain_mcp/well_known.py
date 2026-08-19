"""
`/.well-known/oauth-authorization-server` proxy.

Why this file exists:
  Many IdPs (notably Entra ID) only publish their OpenID Connect discovery
  document under `<issuer>/.well-known/openid-configuration`, the
  "path-appended" OIDC form. They do NOT implement the RFC 8414 §3 path-
  inserted OAuth form (`<host>/.well-known/oauth-authorization-server<path>`)
  that many MCP clients (Blockbrain included) look up.

  To bridge that gap we advertise OURSELVES as the authorization server in the
  Protected Resource Metadata and proxy the upstream IdP's discovery document
  through this endpoint.

  This is a deliberate spec deviation, not a spec-compliant setup. The proxied
  document keeps the upstream `issuer` while the client fetched it from our
  host, and RFC 8414 §3.3 requires those to match. Lenient clients accept it,
  strict ones will reject it. See
  docs/adr/2026-08-18_well-known-authorization-server-proxy.md.

  The tokens are still signed by and validated against the upstream IdP. This
  file only affects *discovery*, not token issuance or verification.

  Scopes belong in our own RFC 9728 Protected Resource Metadata, which
  `auth.py` populates. We inject them here as well, because clients that read
  scopes only from the authorization server document would otherwise ask for
  a token this server cannot accept. Turn that off with
  `MCP_ADVERTISE_SCOPES_IN_AS_METADATA=false`.

In-memory cache with a 10-minute TTL keeps the upstream pressure low.
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import advertised_scopes
from .config import get_settings

log = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 600.0
_cache: dict[str, tuple[float, dict]] = {}


async def _fetch_openid_configuration(url: str) -> dict:
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    _cache[url] = (now, data)
    return data


def _augment_scopes(data: dict, scopes: list[str]) -> dict:
    """Add the resource scopes to `scopes_supported` without duplicating any.

    Entra never advertises custom API scopes in its OIDC discovery document, so
    a client that reads scopes from there asks for `openid profile email` only
    and gets a token for Microsoft Graph rather than for this resource server,
    which then fails audience validation here.
    """
    base = list(data.get("scopes_supported") or [])
    extras = [s for s in dict.fromkeys(scopes) if s not in base]
    return {**data, "scopes_supported": base + extras}


def register_well_known_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> JSONResponse:
        """Proxy the upstream OIDC discovery document."""
        settings = get_settings()
        urls = settings.openid_configuration_urls
        if not urls:
            return JSONResponse({"error": "no upstream issuer configured"}, status_code=500)

        try:
            data = await _fetch_openid_configuration(urls[0])
        except httpx.HTTPError as e:
            log.warning("openid_configuration.fetch_failed", url=urls[0], error=str(e))
            return JSONResponse({"error": "upstream discovery failed"}, status_code=502)

        scopes = advertised_scopes() if settings.mcp_advertise_scopes_in_as_metadata else []
        if scopes:
            # An empty `scopes_supported` claims the AS supports none at all,
            # which is worse for a client than the key being absent.
            data = _augment_scopes(data, scopes)

        return JSONResponse(
            data,
            headers={"Cache-Control": "public, max-age=600"},
        )
