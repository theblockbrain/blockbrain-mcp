"""
Per-request user context extracted from JWT claims and Blockbrain headers.

Different OIDC providers put the user's identity in different claims:
  • Entra ID (v2):      `oid` (stable), `preferred_username`, `email`, `name`, `tid`
  • Zitadel:            `sub`, `urn:zitadel:iam:org:id` (NO `email` in Blockbrain's config)
  • Auth0 / Okta:       `sub`, `email`, `name`
  • Keycloak:           `sub`, `email`, `preferred_username`

We extract a common shape (`UserContext`) from whichever claims are present
and fall back to Blockbrain's `X-Blockbrain-*` headers when the JWT is thin
(that header is injected by the Blockbrain MCP client when `sendUserContext=true`
on the MCP-server config).

⭐ CUSTOMIZE HERE — if your provider uses non-standard claim names, map them
   below.
"""

from dataclasses import dataclass

from fastmcp.server.dependencies import get_access_token, get_http_headers

# Zitadel-specific claim name
ZITADEL_ORG_CLAIM = "urn:zitadel:iam:org:id"

# Entra-specific claims
ENTRA_TENANT_CLAIM = "tid"
ENTRA_OBJECT_ID_CLAIM = "oid"

# Blockbrain-injected headers (sendUserContext=true path)
EMAIL_HEADER = "x-blockbrain-user-email"
ORG_HEADER = "x-blockbrain-org-id"
USER_ID_HEADER = "x-blockbrain-user-id"


@dataclass(frozen=True)
class UserContext:
    user_id: str
    org_id: str | None
    email: str | None
    name: str | None = None


class NotAuthenticatedError(RuntimeError):
    """Raised when a tool is invoked without a valid bearer token."""


def _pick_user_id(claims: dict, headers: dict) -> str | None:
    # Prefer stable provider user IDs, then fall back to sub, then header.
    return claims.get(ENTRA_OBJECT_ID_CLAIM) or claims.get("sub") or headers.get(USER_ID_HEADER)


def _pick_email(claims: dict, headers: dict) -> str | None:
    return (
        claims.get("email")
        or claims.get("preferred_username")  # Entra often puts email here
        or claims.get("upn")
        or headers.get(EMAIL_HEADER)
    )


def _pick_org(claims: dict, headers: dict) -> str | None:
    # Entra tenant ID, Zitadel org ID, or Blockbrain header.
    return (
        claims.get(ENTRA_TENANT_CLAIM) or claims.get(ZITADEL_ORG_CLAIM) or headers.get(ORG_HEADER)
    )


def get_user_context() -> UserContext:
    token = get_access_token()
    if token is None:
        raise NotAuthenticatedError("No access token in request context.")

    claims = token.claims or {}
    headers = {k.lower(): v for k, v in (get_http_headers() or {}).items()}

    user_id = _pick_user_id(claims, headers)
    if not user_id:
        raise NotAuthenticatedError(
            "Token has no user identifier (expected `oid`, `sub`, or X-Blockbrain-User-Id)."
        )

    return UserContext(
        user_id=user_id,
        org_id=_pick_org(claims, headers),
        email=_pick_email(claims, headers),
        name=claims.get("name"),
    )
