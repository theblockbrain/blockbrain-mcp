"""WWW-Authenticate challenges at the HTTP layer (MCP authorization spec).

Drives the real ASGI app with tokens signed by a throwaway RSA key. The
verifier fetches that key as a JWKS document from the configured Entra JWKS
URI through `JWTVerifier`'s public `http_client` hook (a mock transport), so
the whole path is exercised: bearer middleware, JWKS lookup, JWT
verification, scope check, tool call.

  * no token                     -> 401, challenge names the scope to request
                                    and the protected resource metadata URL
  * valid token, wrong/no scope  -> 403 `insufficient_scope` (step-up), not 401
  * valid token with scope       -> tools/list and tools/call go through
  * token for another audience   -> 401 `invalid_token`
"""

from __future__ import annotations

import json
import time
from functools import partial

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier

from blockbrain_mcp.config import get_settings
from blockbrain_mcp.server import build_server

_TENANT = "00000000-0000-0000-0000-000000000001"
_ISSUER = f"https://login.microsoftonline.com/{_TENANT}/v2.0"
_JWKS_URI = f"https://login.microsoftonline.com/{_TENANT}/discovery/v2.0/keys"
_KID = "test-key-1"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWKS = {
    "keys": [
        {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key())),
            "kid": _KID,
            "use": "sig",
            "alg": "RS256",
        }
    ]
}
_RESOURCE_METADATA = (
    'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
)
_SCOPE_CHALLENGE = 'scope="api://blockbrain-mcp/mcp.access"'

_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


def _token(scp: str | None, **overrides: str) -> str:
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "aud": "api://blockbrain-mcp",
        "sub": "user-1",
        "oid": "user-1",
        "tid": _TENANT,
        "preferred_username": "alice@example.com",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        **overrides,
    }
    if scp is not None:
        claims["scp"] = scp
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": _KID})


def _jwks_endpoint(request: httpx2.Request) -> httpx2.Response:
    if str(request.url) != _JWKS_URI:
        return httpx2.Response(404)
    return httpx2.Response(200, json=_JWKS)


async def _post(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    method: str = "tools/list",
    params: dict | None = None,
) -> httpx.Response:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "false")
    monkeypatch.setenv("AZURE_TENANT_ID", _TENANT)
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("MCP_REQUIRED_SCOPES", "mcp.access")
    get_settings.cache_clear()

    jwks_client = httpx2.AsyncClient(transport=httpx2.MockTransport(_jwks_endpoint))
    monkeypatch.setattr(
        "blockbrain_mcp.auth.JWTVerifier", partial(JWTVerifier, http_client=jwks_client)
    )

    app = build_server().http_app(path="/mcp")
    headers = {**_BASE_HEADERS, "Mcp-Method": method}
    if params and "name" in params:
        headers["Mcp-Name"] = params["name"]
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {})}}
    body["params"]["_meta"] = _MODERN_META
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com"
            ) as client,
            app.router.lifespan_context(app),
        ):
            return await client.post("/mcp", json=body, headers=headers)
    finally:
        await jwks_client.aclose()


@pytest.mark.asyncio
async def test_missing_token_challenge_names_scope_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = await _post(monkeypatch, token=None)

    assert resp.status_code == 401
    challenge = resp.headers["WWW-Authenticate"]
    # Entra needs the Application ID URI form, not the bare `scp` value.
    assert _SCOPE_CHALLENGE in challenge
    assert _RESOURCE_METADATA in challenge
    assert "error=" not in challenge  # RFC 6750 §3.1: no error without credentials


@pytest.mark.asyncio
@pytest.mark.parametrize("scp", ["something.else", None], ids=["wrong-scope", "no-scp-claim"])
async def test_token_without_required_scope_gets_step_up_challenge(
    monkeypatch: pytest.MonkeyPatch, scp: str | None
) -> None:
    resp = await _post(monkeypatch, token=_token(scp=scp))

    assert resp.status_code == 403
    challenge = resp.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert _SCOPE_CHALLENGE in challenge
    assert _RESOURCE_METADATA in challenge


@pytest.mark.asyncio
async def test_token_with_required_scope_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _post(monkeypatch, token=_token(scp="mcp.access"))

    assert resp.status_code == 200, resp.text
    assert '"whoami"' in resp.text


@pytest.mark.asyncio
async def test_scoped_token_can_call_a_tool_as_its_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth mode end to end: the identity whoami returns comes from the token."""
    resp = await _post(
        monkeypatch,
        token=_token(scp="mcp.access"),
        method="tools/call",
        params={"name": "whoami", "arguments": {}},
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["user_id"] == "user-1"
    assert result["structuredContent"]["org_id"] == _TENANT
    assert result["structuredContent"]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_token_for_another_audience_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _post(monkeypatch, token=_token(scp="mcp.access", aud="api://someone-else"))

    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]
