# Architecture

## Role in Blockbrain

This project is a **Model Context Protocol (MCP) server** that speaks the
Streamable-HTTP transport (MCP spec 2025-06-18) and authenticates callers as
an **OAuth 2.1 Resource Server**. Blockbrain's integrations service is the
client. It handles the entire OAuth client side — discovery, client
registration (or manual credential entry), authorization-code + PKCE, token
refresh, encrypted storage — so this server never touches authorization
flows directly. It only validates bearer tokens on each request.

```
  ┌──────────────┐      OAuth 2.1 client flow       ┌──────────────────────┐
  │   Blockbrain │ ───────────────────────────────▶ │    IdP (Entra/       │
  │ integrations │ ◀───────────────── tokens ────── │    Zitadel/Auth0/…)  │
  │   service    │                                  │                      │
  └──────┬───────┘                                  └──────────────────────┘
         │
         │  Authorization: Bearer <jwt>
         │  X-Blockbrain-Org-Id / User-Id / User-Email (if sendUserContext=true)
         ▼
  ┌──────────────────────────────────────────────────────────┐
  │ blockbrain-mcp (this project, FastMCP)                   │
  │                                                          │
  │  /.well-known/oauth-protected-resource/mcp (RFC 9728)    │
  │  /mcp                                 (JSON-RPC)         │
  │                                                          │
  │  JWTVerifier  →  tool(user_context)                      │
  └──────────────────────────────────────────────────────────┘
```

Key invariant: **the IdP belongs to the MCP-server operator, not to
Blockbrain.** Blockbrain learns which IdP to trust at runtime via RFC 9728
discovery. That's why `auth.py` is IdP-agnostic.

## Why FastMCP v3

- First-class OAuth 2.1 primitives: `JWTVerifier`, `RemoteAuthProvider`.
- Automatically mounts `/.well-known/oauth-protected-resource/<mcp-path>` —
  exactly what Blockbrain's three-tier discovery expects.
- `JWTVerifier.issuer` accepts `str | list[str]`, which gives us
  multi-tenant Entra support (allow-list of issuer URLs) without custom code.
- Streamable-HTTP transport by default; SSE is deprecated because of the
  2025–2026 CVE cluster affecting SSE-only MCP servers.
- Tool registration via `mcp.tool(func)` makes a registry pattern trivial.

## Why Python 3.12 + uv

- 3.12 is the modern baseline for typed `dict[…]` / `list[…]` syntax and
  modern `structlog`/`pydantic` 2.x.
- `uv` is dramatically faster than pip/poetry and is the default in most
  modern Python projects.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `server.py` | Build the FastMCP instance, register tools, start uvicorn. Thin glue. |
| `config.py` | One typed `Settings` class. Handles all three provider paths (Entra single, Entra multi, generic OIDC). |
| `auth.py` | `RemoteAuthProvider(JWTVerifier(...))` — one function. |
| `context.py` | Turn the JWT + Blockbrain headers into a `UserContext`. Normalizes across Entra/Zitadel/OIDC claim names. |
| `logging_setup.py` | structlog → JSON. |
| `tools/__init__.py` | Tool registry — `ALL_TOOLS` list. |
| `tools/<name>.py` | One tool per file. Each is independent. |

## Config paths in `Settings`

Exactly one of these must be set; a `model_validator` enforces it:

1. **Entra single-tenant:** `AZURE_TENANT_ID=<uuid>` → issuer = `https://login.microsoftonline.com/<uuid>/v2.0`, JWKS = `.../discovery/v2.0/keys`
2. **Entra multi-tenant:** `AZURE_TENANT_ID=common` + `AZURE_ALLOWED_TENANTS=<uuid>,<uuid>` → issuer = list of per-tenant URLs, JWKS = `.../common/...`
3. **Generic OIDC:** `OIDC_ISSUER_URL=<url>` → issuer + JWKS derived; can be overridden with `OIDC_JWKS_URI`

`MCP_RESOURCE_IDENTIFIER` is always required (becomes the `aud` the
verifier enforces). `MCP_PUBLIC_BASE_URL` is required whenever the audience
is not itself an http(s) URL (the Entra case, where the Application ID URI
typically starts with `api://`).

## Tool design conventions

1. **Signature is the contract.** Argument names, types, and the docstring
   become the tool's JSON Schema that MCP clients see.
2. **Return a pydantic `BaseModel`** (not a dict). FastMCP generates a clean
   response schema from it, and it is typed all the way through.
3. **Get user context via `get_user_context()`.** Never pass `user_id` as an
   input — the token is the source of truth.
4. **Handle external failures with typed errors.** Raise with a clear message;
   FastMCP surfaces it to the client.

## Why the tools are what they are

- **`whoami`** is not a product feature; it is a sanity check. If `whoami`
  returns your email, the end-to-end OAuth path is working. Always keep it.
- **`web_search`** / **`read_url`** / **`research_topic`** are realistic tool
  shapes (single API call, external fetch, chained composite). They exist so
  customers can delete them one-by-one without breaking anything else.

## Security posture

- Bearer-token enforcement on every tool call (via FastMCP middleware).
- Audience validation per MCP 2025-06-18 spec (Resource Indicators, RFC 8707).
- Multi-tenant Entra: even though Azure would issue tokens for any tenant,
  the server only accepts `iss` values on the explicit allow-list.
- `X-Blockbrain-*` headers are *inputs*, not trust roots — always prefer the
  JWT claim when both are present.
- Tokens are never logged. If you add logging, scrub them.
- Rate-limits (e.g. `research_topic`) are in-memory for this template; move
  to Redis for production.

## What this template deliberately does **not** do

- No OAuth *client* flow (that lives in Blockbrain).
- No persistent storage (no DB, no Redis).
- No SSE transport (deprecated).
- No Docker/Helm/K8s — customers pick their own deployment target.
- No CI pipeline — a simple GH Actions lint+test job is a one-hour follow-up.
