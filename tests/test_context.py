"""Tests for the user-context extraction across providers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from blockbrain_mcp.context import (
    NotAuthenticatedError,
    UserContext,
    get_user_context,
)


class _FakeToken:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims


def _ctx_with(claims: dict, headers: dict | None = None) -> UserContext:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken(claims)),
        patch("blockbrain_mcp.context.get_http_headers", return_value=headers or {}),
    ):
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
    ctx = _ctx_with(
        {"sub": "zitadel-sub", "urn:zitadel:iam:org:id": "org-1"},
        {"x-blockbrain-user-email": "alice@blockbrain.ai"},
    )
    assert ctx.user_id == "zitadel-sub"
    assert ctx.org_id == "org-1"
    assert ctx.email == "alice@blockbrain.ai"


def test_generic_oidc_claims() -> None:
    ctx = _ctx_with({"sub": "u-1", "email": "alice@example.com", "name": "Alice"})
    assert ctx.user_id == "u-1"
    assert ctx.email == "alice@example.com"
    assert ctx.org_id is None


def test_oid_preferred_over_sub() -> None:
    ctx = _ctx_with({"oid": "entra-oid", "sub": "sub-fallback"})
    assert ctx.user_id == "entra-oid"


def test_upn_as_email_fallback() -> None:
    ctx = _ctx_with({"sub": "u-1", "upn": "alice@contoso.com"})
    assert ctx.email == "alice@contoso.com"


def test_blockbrain_header_fallback_when_sub_missing() -> None:
    ctx = _ctx_with({}, {"x-blockbrain-user-id": "u-from-header"})
    assert ctx.user_id == "u-from-header"


def test_missing_token_raises() -> None:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=None),
        patch("blockbrain_mcp.context.get_http_headers", return_value={}),
        pytest.raises(NotAuthenticatedError),
    ):
        get_user_context()


def test_missing_identity_raises() -> None:
    with (
        patch("blockbrain_mcp.context.get_access_token", return_value=_FakeToken({})),
        patch("blockbrain_mcp.context.get_http_headers", return_value={}),
        pytest.raises(NotAuthenticatedError),
    ):
        get_user_context()
