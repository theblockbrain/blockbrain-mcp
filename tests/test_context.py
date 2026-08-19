"""Tests for the user-context extraction across providers."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from blockbrain_mcp.context import (
    MissingOrgError,
    NotAuthenticatedError,
    UserContext,
    get_user_context,
    require_org_id,
)


class _FakeToken:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims


def _ctx_with(claims: dict) -> UserContext:
    with patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken(claims)):
        return get_user_context()


def test_entra_claims_map_to_context() -> None:
    ctx = _ctx_with(
        {
            "oid": "entra-object-id",
            "tid": "entra-tenant-id",
            "preferred_username": "alice@contoso.com",
            "name": "Alice Example",
        }
    )
    assert ctx == UserContext(
        user_id="entra-object-id",
        org_id="entra-tenant-id",
        email="alice@contoso.com",
        name="Alice Example",
    )


def test_zitadel_claims_map_to_context() -> None:
    ctx = _ctx_with({"sub": "zitadel-sub", "urn:zitadel:iam:org:id": "org-1"})
    assert ctx.user_id == "zitadel-sub"
    assert ctx.org_id == "org-1"


def test_generic_oidc_claims() -> None:
    ctx = _ctx_with({"sub": "u-1", "email": "alice@example.com", "name": "Alice"})
    assert ctx.user_id == "u-1"
    assert ctx.email == "alice@example.com"
    assert ctx.org_id is None


def test_generic_org_claim_is_read() -> None:
    ctx = _ctx_with({"sub": "u-1", "org_id": "acme"})
    assert ctx.org_id == "acme"


def test_oid_preferred_over_sub() -> None:
    ctx = _ctx_with({"oid": "entra-oid", "sub": "sub-fallback"})
    assert ctx.user_id == "entra-oid"


def test_upn_as_email_fallback() -> None:
    ctx = _ctx_with({"sub": "u-1", "upn": "alice@contoso.com"})
    assert ctx.email == "alice@contoso.com"


def test_missing_token_raises() -> None:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=None),
        pytest.raises(NotAuthenticatedError),
    ):
        get_user_context()


def test_missing_identity_raises() -> None:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken({})),
        pytest.raises(NotAuthenticatedError),
    ):
        get_user_context()


# ---------------------------------------------------------------------------
# Caller-supplied headers are never a trust root. `X-Blockbrain-Org-Id` is the
# tenant boundary, so honouring it would let any valid token pick its own org.
# ---------------------------------------------------------------------------


@contextmanager
def _headers_available(headers: dict):
    """Offer the caller's headers by both routes a re-import could take."""
    with (
        patch("fastmcp.server.dependencies.get_http_headers", return_value=headers),
        patch("blockbrain_mcp.context.get_http_headers", return_value=headers, create=True),
    ):
        yield


def _ctx_with_headers(claims: dict, headers: dict) -> UserContext:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken(claims)),
        _headers_available(headers),
    ):
        return get_user_context()


def test_org_header_cannot_supply_a_missing_org_claim() -> None:
    ctx = _ctx_with_headers({"sub": "u-1"}, {"x-blockbrain-org-id": "attacker-org"})
    assert ctx.org_id is None


def test_org_header_cannot_override_the_org_claim() -> None:
    ctx = _ctx_with_headers(
        {"sub": "u-1", "tid": "real-tenant"}, {"x-blockbrain-org-id": "attacker-org"}
    )
    assert ctx.org_id == "real-tenant"


def test_user_id_header_cannot_stand_in_for_a_missing_subject() -> None:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken({})),
        _headers_available({"x-blockbrain-user-id": "u-from-header"}),
        pytest.raises(NotAuthenticatedError),
    ):
        get_user_context()


def test_email_header_cannot_supply_a_missing_email_claim() -> None:
    ctx = _ctx_with_headers({"sub": "u-1"}, {"x-blockbrain-user-email": "spoofed@example.com"})
    assert ctx.email is None


# ---------------------------------------------------------------------------
# Tenant-scoped work fails closed when the token carries no org.
# ---------------------------------------------------------------------------


def test_require_org_id_returns_the_claim() -> None:
    assert require_org_id(_ctx_with({"sub": "u-1", "tid": "tenant-a"})) == "tenant-a"


def test_require_org_id_raises_when_no_org_claim() -> None:
    with pytest.raises(MissingOrgError):
        require_org_id(_ctx_with({"sub": "u-1"}))


# ---------------------------------------------------------------------------
# MCP_AUTH_DISABLED dev-identity fallback, gated strictly on the setting and
# never on "token is None" alone. The tests above run with auth enabled
# (conftest default) and must keep raising. These run with auth explicitly
# disabled and must not.
# ---------------------------------------------------------------------------


def test_missing_token_returns_dev_identity_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blockbrain_mcp.config import get_settings

    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    get_settings.cache_clear()
    with patch("blockbrain_mcp.context.get_access_token", return_value=None):
        ctx = get_user_context()

    assert ctx.user_id == "local-dev"
    assert ctx.email == "dev@localhost"


def test_missing_identity_returns_dev_identity_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blockbrain_mcp.config import get_settings

    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    get_settings.cache_clear()
    with patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken({})):
        ctx = get_user_context()

    assert ctx.user_id == "local-dev"


def test_real_claims_still_win_over_dev_identity_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token that DOES carry claims (e.g. Inspector configured with a real
    bearer anyway) is used as-is. The dev identity is only a fallback for when
    nothing identifies the caller, not a blanket override."""
    from blockbrain_mcp.config import get_settings

    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    get_settings.cache_clear()
    ctx = _ctx_with({"sub": "real-user", "email": "real@example.com"})

    assert ctx.user_id == "real-user"
    assert ctx.email == "real@example.com"
