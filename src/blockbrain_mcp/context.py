"""
Per-request user context, derived only from verified JWT claims.

Different OIDC providers put the user's identity in different claims:
  • Entra ID (v2):      `oid` (stable), `preferred_username`, `email`, `name`, `tid`
  • Zitadel:            `sub`, `urn:zitadel:iam:org:id`
  • Auth0 / Okta:       `sub`, `email`, `name`
  • Keycloak:           `sub`, `email`, `preferred_username`

We extract a common shape (`UserContext`) from whichever claims are present.
The `X-Blockbrain-*` request headers are NOT read: they are set by the caller,
so trusting them would let anyone holding a valid token pick their own user id
or org id. If your provider does not emit an org or e-mail claim, add it to the
token in the provider's scope and claim configuration.

`org_id` is the tenant boundary. It is `None` whenever the token carries no org
claim, and anything scoping data per tenant must call `require_org_id()` so a
missing boundary fails closed instead of reading as "all tenants".

When `MCP_AUTH_DISABLED=true` (local MCP Inspector testing only, see config.py)
and no real identity can be resolved, we return an obviously-fake synthetic dev
identity instead of raising, so identity-dependent tools stay callable without a
token. That fallback is gated STRICTLY on `mcp_auth_disabled`. In the normal
authed path a missing or unidentifiable token still raises
`NotAuthenticatedError`.

⭐ CUSTOMIZE HERE — if your provider uses non-standard claim names, map them
   below.
"""

from dataclasses import dataclass

from fastmcp.server.dependencies import get_access_token

from .config import get_settings

# Zitadel-specific claim name
ZITADEL_ORG_CLAIM = "urn:zitadel:iam:org:id"

# Entra-specific claims
ENTRA_TENANT_CLAIM = "tid"
ENTRA_OBJECT_ID_CLAIM = "oid"

# Common org/tenant claim names used by Auth0, Okta, and Keycloak deployments.
GENERIC_ORG_CLAIMS = ("org_id", "organization_id", "tenant_id")


@dataclass(frozen=True)
class UserContext:
    user_id: str
    org_id: str | None
    email: str | None
    name: str | None = None


class NotAuthenticatedError(RuntimeError):
    """Raised when a tool is invoked without a valid bearer token."""


class MissingOrgError(RuntimeError):
    """Raised when a tenant-scoped operation has no verified org to scope to."""


# Synthetic identity used ONLY when MCP_AUTH_DISABLED=true and no real user
# identity could be resolved. Values are deliberately obvious placeholders,
# never derived from any request data, so they can't be mistaken for a
# genuine user in logs or downstream code.
_DEV_USER_ID = "local-dev"
_DEV_USER_NAME = "Local Dev User (MCP_AUTH_DISABLED)"
_DEV_USER_EMAIL = "dev@localhost"


def _dev_user_context() -> UserContext:
    return UserContext(
        user_id=_DEV_USER_ID,
        org_id=None,
        email=_DEV_USER_EMAIL,
        name=_DEV_USER_NAME,
    )


def _pick_user_id(claims: dict) -> str | None:
    return claims.get(ENTRA_OBJECT_ID_CLAIM) or claims.get("sub")


def _pick_email(claims: dict) -> str | None:
    return (
        claims.get("email")
        or claims.get("preferred_username")  # Entra often puts email here
        or claims.get("upn")
    )


def _pick_org(claims: dict) -> str | None:
    for claim in (ENTRA_TENANT_CLAIM, ZITADEL_ORG_CLAIM, *GENERIC_ORG_CLAIMS):
        value = claims.get(claim)
        if value:
            return str(value)
    return None


def get_user_context() -> UserContext:
    token = get_access_token()
    if token is None:
        # Gated strictly on mcp_auth_disabled, never on "token is None" alone.
        # In the normal authed path a missing token must keep raising.
        if get_settings().mcp_auth_disabled:
            return _dev_user_context()
        raise NotAuthenticatedError("No access token in request context.")

    claims = token.claims or {}

    user_id = _pick_user_id(claims)
    if not user_id:
        if get_settings().mcp_auth_disabled:
            return _dev_user_context()
        raise NotAuthenticatedError(
            "Token has no user identifier (expected an `oid` or `sub` claim)."
        )

    return UserContext(
        user_id=user_id,
        org_id=_pick_org(claims),
        email=_pick_email(claims),
        name=claims.get("name"),
    )


def require_org_id(user: UserContext) -> str:
    """Return the caller's org, or refuse when the token did not carry one."""
    if not user.org_id:
        raise MissingOrgError(
            "Token carries no organisation claim, so this request cannot be "
            "scoped to a tenant. Configure the provider to emit an org claim "
            "(Entra `tid`, Zitadel `urn:zitadel:iam:org:id`, or `org_id`)."
        )
    return user.org_id
