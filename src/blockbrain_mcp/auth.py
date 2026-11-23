"""
OAuth 2.1 Resource Server configuration.

This server only validates bearer tokens issued by an external Authorization
Server. It does not issue tokens itself. That is the posture the MCP
authorization spec (2026-07-28) requires.

⭐ CUSTOMIZE HERE — for most providers you only need to edit `.env`, not this
   file. The JWTVerifier is provider-agnostic. If your provider needs a
   special header or claim transform, extend the builder below.

What this gives us automatically (via RemoteAuthProvider):
   • GET /.well-known/oauth-protected-resource/<mcp-path>   (RFC 9728)
       → JSON pointing Blockbrain / any MCP client at the authorization server,
         including the `scopes_supported` a client needs to ask for.
   • Bearer-token enforcement on every tool call.
   • 401 responses with a WWW-Authenticate header that advertises the
     protected-resource-metadata location (what Blockbrain uses for
     Tier-2 discovery in its `mcp-oauth-service.ts`) and the `scope` a client
     must request. A token that lacks a required scope gets a 403
     `insufficient_scope` challenge naming the scopes (step-up authorization).

We advertise OURSELVES as the authorization server so we can proxy the
customer's OIDC discovery document at /.well-known/oauth-authorization-server,
which Entra does not serve at the RFC 8414 path-inserted URL. That is a
deliberate deviation from RFC 8414 §3.3. See docs/adr/2026-08-18_well-known-
authorization-server-proxy.md.
"""

from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from .config import get_settings

MCP_SPEC_VERSION = "2026-07-28"


def advertised_scopes() -> list[str]:
    """Scopes a client must request, in the shape the provider expects.

    Entra wants the full `<application-id-uri>/<scope>` form. Other providers
    take the bare scope name.
    """
    settings = get_settings()
    resource = settings.audience
    primary = resource[0] if isinstance(resource, list) else resource

    if not settings.required_scopes:
        if settings.mcp_allow_default_scope and primary.startswith("api://"):
            return [f"{primary}/.default"]
        return []

    return [
        f"{primary}/{scope}" if primary.startswith("api://") else scope
        for scope in settings.required_scopes
    ]


def build_auth_provider() -> RemoteAuthProvider:
    settings = get_settings()

    # JWTVerifier.issuer accepts `str | list[str]`. A list means
    # "token must have ONE of these issuers", our multi-tenant Entra path.
    #
    # The verifier checks signature, issuer, audience and expiry only. The
    # required scopes are enforced one layer up, by the provider: a verifier
    # that rejects a scope-less token answers 401 `invalid_token`, which reads
    # to the client as "your token is broken", while the provider answers 403
    # `insufficient_scope` with the missing scopes in the challenge, which is
    # the step-up signal the MCP authorization spec defines.
    verifier = JWTVerifier(
        jwks_uri=settings.jwks_uri,
        issuer=settings.issuer,
        audience=settings.audience,
    )

    scopes = advertised_scopes() or None
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(settings.public_base_url)],
        base_url=AnyHttpUrl(settings.public_base_url),
        scopes_supported=scopes,
        required_scopes=settings.required_scopes or None,
        # The `scope` parameter of the WWW-Authenticate challenge (401, and 403
        # insufficient_scope) tells the client what to request. Entra tokens
        # carry the bare `mcp.access` in `scp`, but the client has to ask for
        # `api://<app>/mcp.access`, so the challenge uses the advertised form.
        challenge_scopes=scopes,
        # RFC 9728 `resource_name`: a human-readable label for consent screens.
        resource_name=settings.mcp_server_name,
    )
