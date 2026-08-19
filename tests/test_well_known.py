"""Tests for where OAuth scopes are advertised."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import FastMCP

from blockbrain_mcp.auth import advertised_scopes, build_auth_provider
from blockbrain_mcp.config import get_settings
from blockbrain_mcp.well_known import _augment_scopes, register_well_known_routes

_OIDC_BASE_SCOPES = ["openid", "profile", "email", "offline_access"]
_BASE_DOC: dict = {"scopes_supported": list(_OIDC_BASE_SCOPES)}


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resource: str = "api://blockbrain-mcp",
    scopes: str = "",
    allow_default: bool = False,
    advertise_in_as: bool = True,
) -> None:
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", resource)
    monkeypatch.setenv("MCP_REQUIRED_SCOPES", scopes)
    monkeypatch.setenv("MCP_ALLOW_DEFAULT_SCOPE", str(allow_default).lower())
    monkeypatch.setenv("MCP_ADVERTISE_SCOPES_IN_AS_METADATA", str(advertise_in_as).lower())
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Scope shape
# ---------------------------------------------------------------------------


def test_entra_scopes_get_the_application_id_uri_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, scopes="mcp.access,documents.read")
    assert advertised_scopes() == [
        "api://blockbrain-mcp/mcp.access",
        "api://blockbrain-mcp/documents.read",
    ]


def test_non_api_resource_scopes_are_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, resource="https://mcp.example.com", scopes="mcp.access")
    assert advertised_scopes() == ["mcp.access"]


# ---------------------------------------------------------------------------
# `.default` asks Entra to bundle every admin-consented scope, so it is opt-in.
# ---------------------------------------------------------------------------


def test_no_scopes_advertised_when_none_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, scopes="")
    assert advertised_scopes() == []


def test_default_scope_only_when_explicitly_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, scopes="", allow_default=True)
    assert advertised_scopes() == ["api://blockbrain-mcp/.default"]


def test_pinned_scopes_never_mix_with_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entra rejects a request mixing `.default` with resource scopes (AADSTS70011)."""
    _configure(monkeypatch, scopes="mcp.access", allow_default=True)
    assert advertised_scopes() == ["api://blockbrain-mcp/mcp.access"]


# ---------------------------------------------------------------------------
# Protected Resource Metadata (RFC 9728) is the home for scopes.
# ---------------------------------------------------------------------------


def test_protected_resource_metadata_carries_the_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, scopes="mcp.access")
    provider = build_auth_provider()
    assert provider._scopes_supported == ["api://blockbrain-mcp/mcp.access"]


# ---------------------------------------------------------------------------
# Authorization server metadata injection is the interop shim.
# ---------------------------------------------------------------------------


def test_scopes_reach_the_authorization_server_document_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_ADVERTISE_SCOPES_IN_AS_METADATA", raising=False)
    get_settings.cache_clear()

    assert get_settings().mcp_advertise_scopes_in_as_metadata is True


def test_augment_adds_scopes_to_the_upstream_document() -> None:
    result = _augment_scopes(dict(_BASE_DOC), ["api://abc/mcp.access"])

    scopes = result["scopes_supported"]
    assert "api://abc/mcp.access" in scopes
    for s in _OIDC_BASE_SCOPES:
        assert s in scopes


def test_augment_does_not_duplicate_existing_scopes() -> None:
    base = {"scopes_supported": [*_OIDC_BASE_SCOPES, "api://abc/mcp.access"]}
    result = _augment_scopes(base, ["api://abc/mcp.access", "api://abc/mcp.access"])

    assert result["scopes_supported"].count("api://abc/mcp.access") == 1


def test_augment_handles_a_document_without_scopes_supported() -> None:
    result = _augment_scopes({}, ["api://abc/mcp.access"])
    assert result["scopes_supported"] == ["api://abc/mcp.access"]


async def _authorization_server_document(monkeypatch: pytest.MonkeyPatch, upstream: dict) -> dict:
    async def _fetch(_url: str) -> dict:
        return dict(upstream)

    monkeypatch.setattr("blockbrain_mcp.well_known._fetch_openid_configuration", _fetch)

    mcp = FastMCP("test")
    register_well_known_routes(mcp)
    app = mcp.http_app(path="/mcp")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_route_injects_the_pinned_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, scopes="mcp.access")
    doc = await _authorization_server_document(monkeypatch, _BASE_DOC)

    assert "api://blockbrain-mcp/mcp.access" in doc["scopes_supported"]


@pytest.mark.asyncio
async def test_route_leaves_the_document_alone_when_the_shim_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, scopes="mcp.access", advertise_in_as=False)
    doc = await _authorization_server_document(monkeypatch, _BASE_DOC)

    assert doc["scopes_supported"] == _OIDC_BASE_SCOPES


@pytest.mark.asyncio
async def test_route_never_advertises_an_empty_scope_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream document without `scopes_supported` must not gain an empty one."""
    _configure(monkeypatch, scopes="")
    doc = await _authorization_server_document(monkeypatch, {"issuer": "https://idp.example.com"})

    assert "scopes_supported" not in doc
