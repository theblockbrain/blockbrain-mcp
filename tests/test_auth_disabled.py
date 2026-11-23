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
from blockbrain_mcp.server import UnsafeAuthDisabledConfigError, build_server, main

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}
_TOOLS_LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_WHOAMI_CALL_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "whoami", "arguments": {}},
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


async def _post_via_handshake(body: dict) -> httpx.Response:
    """Drive the legacy (2025-11-25 and earlier) streamable-HTTP handshake (initialize -> `body`), with
    no Authorization header on either request, and return the response to
    the second request.

    A rejecting server can 401 on `initialize` itself; a permissive one needs
    the session-id round-trip to get further at all — so we always complete
    the handshake and only assert on the final response.
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
        return await client.post("/mcp", json=body, headers=session_headers)


async def _post_tools_list(monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    return await _post_via_handshake(_TOOLS_LIST_BODY)


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
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
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


def test_auth_disabled_on_non_localhost_host_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP_AUTH_DISABLED=true + a network-reachable MCP_HOST must fail closed.

    A server bound to e.g. the Docker-default 0.0.0.0 with auth off would be
    fully unauthenticated and reachable from outside the machine -- main()
    must refuse to start (before ever binding a socket) rather than only warn.
    """
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    get_settings.cache_clear()

    with pytest.raises(UnsafeAuthDisabledConfigError, match="not localhost-only"):
        main()


def test_build_server_does_not_check_bind_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_server() only assembles the app; it never binds, so it must not
    hard-fail on MCP_HOST -- an embedder may build the server and mount
    http_app() on an address of its own choosing.
    """
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    get_settings.cache_clear()

    build_server()  # must not raise


def test_auth_disabled_does_not_register_well_known_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """No upstream IdP configured/contacted in auth-disabled mode -> no proxy route."""
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    get_settings.cache_clear()
    app = build_server().http_app(path="/mcp")
    paths = [getattr(r, "path", "") for r in app.routes]
    assert not any("oauth-authorization-server" in p for p in paths), paths


@pytest.mark.asyncio
async def test_auth_disabled_whoami_call_returns_dev_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP_AUTH_DISABLED=true -> identity-dependent tools are actually callable

    (not just tools/list): whoami must succeed with a synthetic, obviously-fake
    dev identity instead of raising NotAuthenticatedError.
    """
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    resp = await _post_via_handshake(_WHOAMI_CALL_BODY)

    assert resp.status_code == 200
    body = resp.text
    assert '"isError":false' in body
    assert '"user_id":"local-dev"' in body
    assert '"email":"dev@localhost"' in body


# ---------------------------------------------------------------------------
# 2026-07-28: no handshake, no session. Every request carries its own
# protocol version and client capabilities in `_meta`.
# ---------------------------------------------------------------------------

_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "0"},
}


async def _post_stateless(method: str, params: dict | None = None) -> httpx.Response:
    get_settings.cache_clear()
    app = build_server().http_app(path="/mcp")
    headers = {**_MCP_HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if params and "name" in params:
        headers["Mcp-Name"] = params["name"]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), "_meta": _MODERN_META},
    }
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        return await client.post("/mcp", json=body, headers=headers)


@pytest.mark.asyncio
async def test_stateless_protocol_serves_discover_with_server_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    resp = await _post_stateless("server/discover")

    assert resp.status_code == 200, resp.text
    assert "mcp-session-id" not in resp.headers
    result = resp.json()["result"]
    assert "2026-07-28" in result["supportedVersions"]
    server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert server_info["name"] == get_settings().mcp_server_name
    assert server_info["version"] != "0.0.0"


@pytest.mark.asyncio
async def test_stateless_protocol_lists_tools_with_titles_and_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    resp = await _post_stateless("tools/list")

    assert resp.status_code == 200, resp.text
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    assert list(tools) == ["whoami", "web_search", "read_url", "research_topic"]
    for tool in tools.values():
        assert tool["title"]
        assert tool["annotations"]["readOnlyHint"] is True
        assert "outputSchema" in tool
    assert tools["whoami"]["annotations"]["openWorldHint"] is False
    assert tools["read_url"]["annotations"]["openWorldHint"] is True


@pytest.mark.asyncio
async def test_stateless_protocol_calls_a_tool_without_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    resp = await _post_stateless("tools/call", {"name": "whoami", "arguments": {}})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["user_id"] == "local-dev"


def test_main_enables_origin_guard_only_when_auth_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport spec requires Origin validation. With auth off (loopback
    only) FastMCP's guard runs in "auto" mode; with auth on it stays off so a
    tunnel or reverse proxy forwarding the public Host keeps working."""
    from fastmcp import FastMCP

    captured: list[dict] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, **kw: captured.append(kw))

    monkeypatch.setenv("MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    get_settings.cache_clear()
    main()
    monkeypatch.setenv("MCP_AUTH_DISABLED", "false")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    get_settings.cache_clear()
    main()

    assert [kw["host_origin_protection"] for kw in captured] == ["auto", False]
    # Every run path (make dev, make serve, the Docker entrypoint) goes through
    # main(), so this is the one place the banner has to be turned off.
    assert all(kw["show_banner"] is False for kw in captured)
