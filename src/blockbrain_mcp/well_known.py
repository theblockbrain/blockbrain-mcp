"""
`/.well-known/oauth-authorization-server` proxy.

Why this file exists:
  Many IdPs (notably Entra ID) only publish their OpenID Connect discovery
  document under `<issuer>/.well-known/openid-configuration` — the
  "path-appended" OIDC form. They do NOT implement the RFC 8414 §3 path-
  inserted OAuth form (`<host>/.well-known/oauth-authorization-server<path>`)
  that many MCP clients (Blockbrain included)
  look up.

  To bridge this gap, we advertise OURSELVES as the authorization server in
  the Protected Resource Metadata and proxy the upstream IdP's discovery
  document through this endpoint. The two specs are compatible: the OpenID
  Connect discovery document is a superset of RFC 8414, so passing it
  through unchanged works.

  The tokens are still signed by and validated against the upstream IdP —
  this file only affects *discovery*, not token issuance or verification.

In-memory cache with a 10-minute TTL keeps the upstream pressure low.
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

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


def _augment_scopes(data: dict, settings) -> dict:
    """Inject the resource-prefixed scopes into `scopes_supported`.

    Entra ID never advertises custom API scopes in its OIDC discovery
    document — the document only lists `openid profile email offline_access`.
    Without this augmentation, MCP clients (Blockbrain) read those scopes,
    request a token for them, and Entra issues a token for Microsoft Graph
    instead of for OUR resource server — which then fails JWT audience
    validation on this server.

    For Entra, the right scope shape is `<application-id-uri>/<scope-name>`
    (e.g. `api://<client-id>/mcp.access`). We compute that from the configured
    `MCP_RESOURCE_IDENTIFIER` + `MCP_REQUIRED_SCOPES`.

    Scope selection logic:
    - When `MCP_REQUIRED_SCOPES` is set: emit ONLY the named resource scopes.
      Entra rejects requests that mix `.default` with resource-specific scopes
      (AADSTS70011: "The provided value for the input parameter 'scope' is not
      valid"). Emitting both would break token acquisition for clients that send
      all advertised scopes.
    - When `MCP_REQUIRED_SCOPES` is empty: fall back to `<resource>/.default`,
      which asks Entra to bundle all admin-consented scopes for the resource —
      a safe catch-all for deployments that haven't pinned their scope list yet.

    Standard OIDC scopes (`openid`, `profile`, `email`, `offline_access`) carry
    no resource prefix and are never emitted here; they come from the upstream
    discovery document unchanged.
    """
    base = list(data.get("scopes_supported") or [])
    resource = settings.audience  # str | list[str]
    primary = resource[0] if isinstance(resource, list) else resource

    if settings.required_scopes:
        # Named scopes only — must NOT include .default (Entra AADSTS70011).
        extras: list[str] = []
        for s in settings.required_scopes:
            full = f"{primary}/{s}" if primary.startswith("api://") else s
            if full not in base and full not in extras:
                extras.append(full)
    else:
        # No scopes pinned — advertise .default as a safe catch-all.
        default_scope = f"{primary}/.default"
        extras = [] if default_scope in base else [default_scope]

    return {**data, "scopes_supported": base + extras}


def register_well_known_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> JSONResponse:
        """Proxy upstream OIDC discovery, augmented with our resource scopes."""
        settings = get_settings()
        urls = settings.openid_configuration_urls
        if not urls:
            return JSONResponse({"error": "no upstream issuer configured"}, status_code=500)

        try:
            data = await _fetch_openid_configuration(urls[0])
        except httpx.HTTPError as e:
            log.warning("openid_configuration.fetch_failed", url=urls[0], error=str(e))
            return JSONResponse({"error": "upstream discovery failed"}, status_code=502)

        augmented = _augment_scopes(data, settings)
        return JSONResponse(
            augmented,
            headers={"Cache-Control": "public, max-age=600"},
        )
