# Architecture

## Role in Blockbrain

This project is a **Model Context Protocol (MCP) server** that speaks the
Streamable-HTTP transport (MCP spec 2026-07-28) and authenticates callers as
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

## Why FastMCP v4

`pyproject.toml` pins `fastmcp>=4.2.5,<5`, which builds on the MCP Python SDK
v2.

- Speaks MCP 2026-07-28 (stateless requests, `server/discover`, no
  `initialize` handshake, no `Mcp-Session-Id`) and still serves 2025-era
  clients (`initialize` + session) from the same `/mcp` endpoint. The version
  is negotiated per request, so nothing here pins a protocol version.

- First-class OAuth 2.1 primitives: `JWTVerifier`, `RemoteAuthProvider`.
- Automatically mounts `/.well-known/oauth-protected-resource/<mcp-path>`,
  exactly what Blockbrain's three-tier discovery expects, including the
  `scopes_supported` field this server populates.
- `JWTVerifier.issuer` accepts `str | list[str]`, which gives us
  multi-tenant Entra support (allow-list of issuer URLs) without custom code.
- Streamable-HTTP transport by default. The old HTTP+SSE transport is
  formally Deprecated in 2026-07-28 and is not used here.
- `insufficient_scope` step-up challenges (403) and the `scope` parameter in
  `WWW-Authenticate`, both driven by `RemoteAuthProvider`.
- Tool registration via `mcp.tool(func)` makes a registry pattern trivial.

## Why Python 3.14 + uv

- `.python-version` and both Dockerfile stages use Python 3.14, so CI tests
  on the interpreter that ships. `requires-python` stays at `>=3.12` for
  embedders on older interpreters.
- `uv` is dramatically faster than pip/poetry and is the default in most
  modern Python projects.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `server.py` | Build the FastMCP instance, register tools, start uvicorn. Thin glue. |
| `well_known.py` | `/.well-known/oauth-authorization-server` proxy (see the ADR). |
| `config.py` | One typed `Settings` class. Handles all three provider paths (Entra single, Entra multi, generic OIDC). |
| `auth.py` | `RemoteAuthProvider(JWTVerifier(...))` — one function. |
| `context.py` | Turn verified JWT claims into a `UserContext`. Normalizes across Entra/Zitadel/OIDC claim names. |
| `url_guard.py` | Scheme, address-range and size checks on every outbound fetch, on each redirect hop. |
| `logging_setup.py` | structlog → JSON, plus request correlation and field redaction. |
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
- Audience validation per the MCP spec (Resource Indicators, RFC 8707).
- Multi-tenant Entra: even though Azure would issue tokens for any tenant,
  the server only accepts `iss` values on the explicit allow-list.
- **Identity and org come from verified token claims only.** The
  `X-Blockbrain-*` request headers are set by the caller and are never read.
  `org_id` is the tenant boundary, so a token without an org claim yields
  `org_id=None` and `require_org_id()` refuses the request rather than
  treating it as unscoped. If your provider does not emit an org or e-mail
  claim, add it in the provider's scope and claim configuration.
- **Outbound fetches are guarded.** `read_url` and `research_topic` accept
  https only, reject private, loopback, link-local and other reserved address
  ranges, re-check every redirect hop, cap the response size, and cap the
  extracted text handed to the model. The connection is pinned to the exact
  address that was validated, closing the DNS-rebinding gap where a host
  changes what it resolves to between the check and the connection.
  `READ_URL_ALLOWED_DOMAINS` narrows it further to known hosts.
- Page content fetched by a tool is untrusted input that reaches the model's
  context. Treat it as data, never as instructions.
- **Logs are redacted and correlated.** Secrets are dropped, e-mail local
  parts are masked, URL query strings are stripped, and every line carries a
  `request_id` taken from the incoming `traceparent` or `X-Request-Id`.
- Rate-limits (e.g. `research_topic`) are in-memory for this template. Move
  them to Redis for production.

## MCP 2026-07-28 coverage

Handled by FastMCP 4 / MCP SDK v2 (nothing to configure): the stateless
core (no `initialize`, no `Mcp-Session-Id`, protocol version and client
capabilities in `_meta`), `server/discover`, the `MCP-Protocol-Version`,
`Mcp-Method` and `Mcp-Name` headers, `resultType` and the `ttlMs` /
`cacheScope` cache hints, `subscriptions/listen` in place of the GET stream,
the renumbered error codes, and the 4 MiB request body cap.

Configured in this template: RFC 9728 Protected Resource Metadata with
`scopes_supported` and `resource_name`; the `scope` parameter in every
`WWW-Authenticate` challenge, in the form the IdP expects
(`api://<app>/mcp.access` for Entra); 403 `insufficient_scope` for a valid
token that lacks a required scope; audience binding (RFC 8707); tool
`title`, annotations and output schemas; `serverInfo.version`; and the Host /
Origin guard (403 on a foreign `Origin`) in the localhost-only
`MCP_AUTH_DISABLED` mode.

Not addressed here:

- Client ID Metadata Documents (which replace deprecated Dynamic Client
  Registration), `application_type`, RFC 9207 `iss` validation and binding
  client credentials to their issuer are client- or IdP-side, so Blockbrain
  and the IdP own them.
- Multi round-trip requests, the tasks extension, and the deprecated
  Sampling, Roots and Logging features: no tool here needs them.
- The Host / Origin guard stays off with auth enabled. A rebinding page
  cannot attach the bearer token, and a strict Host check would reject the
  public hostname a tunnel or reverse proxy forwards. Put it in front of the
  server (reverse proxy allow-list) if your deployment needs it.
- The `/.well-known/oauth-authorization-server` proxy is a deliberate RFC 8414
  §3.3 deviation. See
  [ADR 2026-08-18-well-known-authorization-server-proxy](adr/2026-08-18_well-known-authorization-server-proxy.md).

## What ships alongside the server

- `Dockerfile` (multi-stage, non-root) and `compose.yaml` pinned to a released
  image tag. No Helm chart or K8s manifests: customers pick their own target.
- GitHub Actions: `ci.yml` runs ruff and pytest and audits dependencies for
  known advisories. `docker.yml` runs after `ci.yml`, builds the image
  locally, starts it and checks that it answers HTTP, scans it with Trivy,
  and only then logs in and pushes (never on pull requests), with an SBOM and
  provenance. Workflows are read-only by default, only the build job gets
  `packages: write`, checkouts do not keep the token, and every action is
  pinned to a commit SHA.
- Dependabot for uv, GitHub Actions, and Docker. Both Dockerfile stages use
  the same Python image, so they always move together. Python minor/major
  bumps are manual because `.python-version` has to move with them.

## What this template deliberately does **not** do

- No OAuth *client* flow (that lives in Blockbrain).
- No persistent storage (no DB, no Redis).
- No HTTP+SSE transport (Deprecated in the spec).
- No Helm chart or K8s manifests.
