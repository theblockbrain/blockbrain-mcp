"""WWW-Authenticate challenges at the HTTP layer (MCP authorization spec).

Drives the real ASGI app with tokens signed by a throwaway RSA key, so the
whole path is exercised: bearer middleware, JWT verification, scope check.

  * no token                -> 401, challenge names the scope to request and
                               the protected resource metadata URL
  * valid token, no scope   -> 403 `insufficient_scope` (step-up), not 401
  * valid token with scope  -> request goes through
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from blockbrain_mcp.config import get_settings
from blockbrain_mcp.server import build_server

_TENANT = "00000000-0000-0000-0000-000000000001"
_ISSUER = f"https://login.microsoftonline.com/{_TENANT}/v2.0"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)

_TOOLS_LIST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/list",
}


def _token(scp: str | None) -> str:
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "aud": "api://blockbrain-mcp",
        "sub": "user-1",
        "oid": "user-1",
        "tid": _TENANT,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    if scp is not None:
        claims["scp"] = scp
    return jwt.encode(claims, _KEY, algorithm="RS256")


async def _post(monkeypatch: pytest.MonkeyPatch, token: str | None) -> httpx.Response:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "false")
    monkeypatch.setenv("AZURE_TENANT_ID", _TENANT)
    monkeypatch.setenv("MCP_RESOURCE_IDENTIFIER", "api://blockbrain-mcp")
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("MCP_REQUIRED_SCOPES", "mcp.access")
    get_settings.cache_clear()

    server = build_server()
    verifier = server.auth.token_verifier  # type: ignore[union-attr]

    async def _key(_token: str) -> bytes:
        return _PUBLIC_PEM

    monkeypatch.setattr(verifier, "_get_verification_key", _key)

    app = server.http_app(path="/mcp")
    headers = dict(_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com"
        ) as client,
        app.router.lifespan_context(app),
    ):
        return await client.post("/mcp", json=_TOOLS_LIST, headers=headers)


@pytest.mark.asyncio
async def test_missing_token_challenge_names_scope_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = await _post(monkeypatch, token=None)

    assert resp.status_code == 401
    challenge = resp.headers["WWW-Authenticate"]
    # Entra needs the Application ID URI form, not the bare `scp` value.
    assert 'scope="api://blockbrain-mcp/mcp.access"' in challenge
    assert (
        'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert "error=" not in challenge  # RFC 6750 §3.1: no error without credentials


@pytest.mark.asyncio
async def test_token_without_required_scope_gets_step_up_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = await _post(monkeypatch, token=_token(scp="something.else"))

    assert resp.status_code == 403
    challenge = resp.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="api://blockbrain-mcp/mcp.access"' in challenge


@pytest.mark.asyncio
async def test_token_with_required_scope_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _post(monkeypatch, token=_token(scp="mcp.access"))

    assert resp.status_code == 200, resp.text
    assert '"whoami"' in resp.text


@pytest.mark.asyncio
async def test_token_for_another_audience_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    other = jwt.encode(
        {**jwt.decode(_token("mcp.access"), options={"verify_signature": False}), "aud": "x"},
        _KEY,
        algorithm="RS256",
    )
    resp = await _post(monkeypatch, token=other)

    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]
