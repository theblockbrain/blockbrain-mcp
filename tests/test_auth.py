"""Smoke tests for the auth-provider wiring."""

from __future__ import annotations

import pytest
from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier

from blockbrain_mcp.auth import build_auth_provider
from blockbrain_mcp.config import Settings


def _entra_single_env(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AZURE_ALLOWED_TENANTS", "")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    monkeypatch.setenv("OIDC_JWKS_URI", "")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")


def test_build_auth_provider_returns_remote_provider() -> None:
    provider = build_auth_provider()
    assert isinstance(provider, RemoteAuthProvider)


def test_entra_single_tenant_derives_issuer_and_jwks(monkeypatch) -> None:
    _entra_single_env(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.is_entra is True
    assert s.issuer == "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/v2.0"
    assert s.jwks_uri.endswith("/discovery/v2.0/keys")
    # `api://<id>` audience expands to BOTH forms (v1 URI and v2 bare GUID).
    assert s.audience == ["api://blockbrain-mcp", "blockbrain-mcp"]


def test_entra_multi_tenant_builds_issuer_list(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("AZURE_ALLOWED_TENANTS", "tenant-a, tenant-b")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    s = Settings()  # type: ignore[call-arg]
    assert s.issuer == [
        "https://login.microsoftonline.com/tenant-a/v2.0",
        "https://login.microsoftonline.com/tenant-b/v2.0",
    ]


def test_entra_common_without_allow_list_raises(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("AZURE_ALLOWED_TENANTS", "")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    s = Settings()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="AZURE_ALLOWED_TENANTS"):
        _ = s.issuer


def test_generic_oidc_provider(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.example.com")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "https://mcp.example.com")
    s = Settings()  # type: ignore[call-arg]
    assert s.is_entra is False
    assert s.issuer == "https://auth.example.com"
    assert s.jwks_uri == "https://auth.example.com/.well-known/jwks.json"


def test_both_providers_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.example.com")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    with pytest.raises(ValueError, match="EITHER"):
        Settings()  # type: ignore[call-arg]


def test_no_provider_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    with pytest.raises(ValueError, match="No provider"):
        Settings()  # type: ignore[call-arg]


def test_well_known_routes_present() -> None:
    provider = build_auth_provider()
    routes = provider.get_routes(mcp_path="/mcp")
    paths = [getattr(r, "path", "") for r in routes]
    assert any("oauth-protected-resource" in p for p in paths), paths


def test_verifier_is_jwt_verifier() -> None:
    provider = build_auth_provider()
    assert isinstance(provider.token_verifier, JWTVerifier)
