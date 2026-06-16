"""
OAuth 2.1 Resource Server configuration.

This server only validates bearer tokens issued by an external Authorization
Server — it does not issue tokens itself. That is the posture the MCP
authorization spec (2025-06-18) requires.

⭐ CUSTOMIZE HERE — for most providers you only need to edit `.env`, not this
   file. The JWTVerifier is provider-agnostic. If your provider needs a
   special header, claim transform, or SSRF allow-list, extend the builder
   below.

What this gives us automatically (via RemoteAuthProvider):
   • GET /.well-known/oauth-protected-resource/<mcp-path>   (RFC 9728)
       → JSON pointing Blockbrain / any MCP client at the authorization server.
   • Bearer-token enforcement on every tool call.
   • 401 responses with a WWW-Authenticate header that advertises the
     protected-resource-metadata location (what Blockbrain uses for
     Tier-2 discovery in its `mcp-oauth-service.ts`).
"""

from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from .config import get_settings


def build_auth_provider() -> RemoteAuthProvider:
    settings = get_settings()

    # JWTVerifier.issuer accepts `str | list[str]`. A list means
    # "token must have ONE of these issuers" — our multi-tenant Entra path.
    verifier = JWTVerifier(
        jwks_uri=settings.jwks_uri,
        issuer=settings.issuer,
        audience=settings.audience,
        required_scopes=settings.required_scopes or None,
    )

    # We advertise OURSELVES as the authorization server in the Protected
    # Resource Metadata. That lets us sit in front of whatever OIDC provider
    # the customer is using and proxy its Authorization Server Metadata at
    # /.well-known/oauth-authorization-server. This is the workaround for
    # Entra — which does not serve the RFC 8414 path-inserted metadata URL
    # that many MCP clients (including Blockbrain) look up.
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(settings.public_base_url)],
        base_url=AnyHttpUrl(settings.public_base_url),
    )
