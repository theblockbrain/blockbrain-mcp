"""
`whoami` — returns the identity of the caller as derived from the bearer token.

Keep this tool in production. It is invaluable when debugging auth wiring:
if `whoami` returns your email, the OAuth flow is working end-to-end.
"""

from pydantic import BaseModel

from ._base import get_user_context


class WhoAmIResult(BaseModel):
    user_id: str
    org_id: str | None
    email: str | None
    message: str


async def whoami() -> WhoAmIResult:
    """Return the authenticated user's identity (from OAuth 2.1 bearer token + Blockbrain headers)."""
    ctx = get_user_context()
    return WhoAmIResult(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        email=ctx.email,
        message=f"You are authenticated as {ctx.email or ctx.user_id}.",
    )
