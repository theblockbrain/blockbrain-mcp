"""End-to-end coverage for the MCP_AUTH_DISABLED escape hatch.

Drives the real ASGI app (no mocked transport) to prove the two states
actually differ at the HTTP layer:
  * default (auth enforced)  -> unauthenticated tools/list call is rejected (401)
  * MCP_AUTH_DISABLED=true   -> the same unauthenticated call goes through (200)
"""

from __future__ import annotations

import httpx
import pytest

from blockbrain_mcp.config import get_settings
from blockbrain_mcp.server import build_server

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}
_TOOLS_LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


async def _post_tools_list(monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """Drive the real streamable-HTTP handshake (initialize -> tools/list),
    with no Authorization header on either request, and return the response
    to the tools/list call.

    A rejecting server can 401 on `initialize` itself; a permissive one needs
    the session-id round-trip to get to `tools/list` at all — so we always
    complete the handshake and only assert on the final response.
    """
    get_settings.cache_clear()
    app = build_server().http_app(path="/mcp")
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        init_resp = await client.post("/mcp", json=_INIT_BODY, headers=_MCP_HEADERS)
        if init_resp.status_code != 200:
            return init_resp

        session_headers = {**_MCP_HEADERS, "mcp-session-id": init_resp.headers["mcp-session-id"]}
        return await client.post("/mcp", json=_TOOLS_LIST_BODY, headers=session_headers)


@pytest.mark.asyncio
async def test_default_rejects_unauthenticated_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default config (auth enforced, conftest env) -> 401 without a bearer token."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "false")
    resp = await _post_tools_list(monkeypatch)
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_auth_disabled_allows_unauthenticated_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_AUTH_DISABLED=true -> the same request is no longer rejected for lack of a token."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    resp = await _post_tools_list(monkeypatch)
    assert resp.status_code == 200
    assert "WWW-Authenticate" not in resp.headers


def test_auth_disabled_skips_provider_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    """With auth disabled, no AZURE_TENANT_ID / OIDC_ISSUER_URL is required at all."""
    from blockbrain_mcp.config import Settings

    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "")
    monkeypatch.setenv("OIDC_ISSUER_URL", "")
    s = Settings()  # type: ignore[call-arg]
    assert s.mcp_auth_disabled is True


def test_auth_disabled_does_not_register_well_known_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """No upstream IdP configured/contacted in auth-disabled mode -> no proxy route."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    get_settings.cache_clear()
    app = build_server().http_app(path="/mcp")
    paths = [getattr(r, "path", "") for r in app.routes]
    assert not any("oauth-authorization-server" in p for p in paths), paths
