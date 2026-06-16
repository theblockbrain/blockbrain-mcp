"""Tests for well_known._augment_scopes scope injection logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from blockbrain_mcp.well_known import _augment_scopes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OIDC_BASE_SCOPES = ["openid", "profile", "email", "offline_access"]
_BASE_DOC: dict = {"scopes_supported": list(_OIDC_BASE_SCOPES)}


def _settings(
    resource_identifier: str,
    required_scopes: list[str],
) -> MagicMock:
    """Build a minimal settings stub for _augment_scopes."""
    mock = MagicMock()
    # audience mirrors Settings.audience logic for api:// URIs
    primary = resource_identifier.rstrip("/")
    if primary.startswith("api://"):
        mock.audience = [primary, primary[len("api://") :]]
    else:
        mock.audience = primary
    mock.required_scopes = required_scopes
    return mock


# ---------------------------------------------------------------------------
# Case 1: required_scopes non-empty — named scopes only, NO .default
# ---------------------------------------------------------------------------


def test_required_scopes_single_emits_prefixed_scope_not_default() -> None:
    """Single required scope → only `api://<id>/mcp.access` in output, no .default."""
    settings = _settings("api://abc-client-id", ["mcp.access"])
    result = _augment_scopes(dict(_BASE_DOC), settings)

    scopes = result["scopes_supported"]
    assert "api://abc-client-id/mcp.access" in scopes
    assert "api://abc-client-id/.default" not in scopes
    # Standard OIDC scopes must still be present (unchanged from upstream doc)
    for s in _OIDC_BASE_SCOPES:
        assert s in scopes


def test_required_scopes_multiple_all_prefixed_no_default() -> None:
    """Multiple required scopes → all prefixed, still no .default."""
    settings = _settings("api://abc-client-id", ["mcp.access", "documents.read"])
    result = _augment_scopes(dict(_BASE_DOC), settings)

    scopes = result["scopes_supported"]
    assert "api://abc-client-id/mcp.access" in scopes
    assert "api://abc-client-id/documents.read" in scopes
    assert "api://abc-client-id/.default" not in scopes


# ---------------------------------------------------------------------------
# Case 2: required_scopes empty — .default as fallback
# ---------------------------------------------------------------------------


def test_empty_required_scopes_emits_default() -> None:
    """No required scopes → `api://<id>/.default` advertised."""
    settings = _settings("api://abc-client-id", [])
    result = _augment_scopes(dict(_BASE_DOC), settings)

    scopes = result["scopes_supported"]
    assert "api://abc-client-id/.default" in scopes
    # No spurious resource-specific scopes invented
    assert not any(
        s.startswith("api://abc-client-id/") and s != "api://abc-client-id/.default" for s in scopes
    )


# ---------------------------------------------------------------------------
# Case 3: non-api:// resource (generic OIDC) — scopes not prefixed
# ---------------------------------------------------------------------------


def test_non_api_resource_scopes_not_prefixed() -> None:
    """For non-api:// resources, scope names are used verbatim (no prefix)."""
    settings = _settings("https://mcp.example.com", ["mcp.access"])
    result = _augment_scopes(dict(_BASE_DOC), settings)

    scopes = result["scopes_supported"]
    assert "mcp.access" in scopes
    # No api:// prefix applied
    assert not any(s.startswith("api://") for s in scopes)


# ---------------------------------------------------------------------------
# Case 4: deduplication — already-present scopes not doubled
# ---------------------------------------------------------------------------


def test_no_duplicate_scopes_when_already_in_base() -> None:
    """Scopes already in the upstream doc must not appear twice."""
    base_doc = {"scopes_supported": [*_OIDC_BASE_SCOPES, "api://abc-client-id/mcp.access"]}
    settings = _settings("api://abc-client-id", ["mcp.access"])
    result = _augment_scopes(base_doc, settings)

    scopes = result["scopes_supported"]
    assert scopes.count("api://abc-client-id/mcp.access") == 1


def test_default_not_doubled_when_already_in_base() -> None:
    """.default already in upstream doc → not duplicated when required_scopes is empty."""
    base_doc = {"scopes_supported": [*_OIDC_BASE_SCOPES, "api://abc-client-id/.default"]}
    settings = _settings("api://abc-client-id", [])
    result = _augment_scopes(base_doc, settings)

    scopes = result["scopes_supported"]
    assert scopes.count("api://abc-client-id/.default") == 1


# ---------------------------------------------------------------------------
# Case 5: missing scopes_supported in upstream doc
# ---------------------------------------------------------------------------


def test_handles_missing_scopes_supported_key() -> None:
    """Upstream doc without scopes_supported still produces a valid result."""
    settings = _settings("api://abc-client-id", ["mcp.access"])
    result = _augment_scopes({}, settings)

    scopes = result["scopes_supported"]
    assert "api://abc-client-id/mcp.access" in scopes


# ---------------------------------------------------------------------------
# Integration: env-driven settings path (via conftest defaults)
# ---------------------------------------------------------------------------


def test_env_settings_with_required_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: real Settings object with MCP_REQUIRED_SCOPES set."""
    monkeypatch.setenv("MCP_REQUIRED_SCOPES", "mcp.access")
    # Reload settings so the new env var takes effect (conftest clears cache)
    from blockbrain_mcp.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    result = _augment_scopes(dict(_BASE_DOC), settings)
    scopes = result["scopes_supported"]

    assert "api://blockbrain-mcp/mcp.access" in scopes
    assert "api://blockbrain-mcp/.default" not in scopes


def test_env_settings_without_required_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: real Settings object with MCP_REQUIRED_SCOPES empty → .default.

    We set the env var to "" rather than deleting it, because pydantic-settings
    also reads the .env file on disk (which may have MCP_REQUIRED_SCOPES=mcp.access
    for local development). An explicit empty env var takes priority over the .env
    file value.
    """
    monkeypatch.setenv("MCP_REQUIRED_SCOPES", "")
    # Use the same resource identifier as the conftest default so audience lines up.
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    from blockbrain_mcp.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    result = _augment_scopes(dict(_BASE_DOC), settings)
    scopes = result["scopes_supported"]

    assert "api://blockbrain-mcp/.default" in scopes
    assert not any(
        s.startswith("api://blockbrain-mcp/") and s != "api://blockbrain-mcp/.default"
        for s in scopes
    )
