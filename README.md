# blockbrain-mcp

A **Python MCP server template** that plugs into Blockbrain as an
**OAuth 2.1 Resource Server**. The default provider is **Entra ID (Azure AD)**
because that is what most Blockbrain customers already operate — but the
template is generic OIDC underneath and works identically with Zitadel,
Auth0, Okta, Keycloak, and friends.

What you get:

- Automatic RFC 9728 `/.well-known/oauth-protected-resource` endpoint —
  Blockbrain's `mcp-oauth-service.ts` auto-discovers it
- JWT validation (signature, issuer, audience, expiry, scopes) against the
  provider's JWKS — configured from env vars, zero custom crypto
- Entra single-tenant **and** multi-tenant modes (allow-list)
- Modular tool registry — `whoami`, `web_search`, `read_url`, `research_topic`
  as swap-out examples
- `⭐ CUSTOMIZE HERE` markers at every extension point
- 23 unit tests + local cloudflared tunnel integration

---

## Quickstart

### Option A — Docker Compose (recommended for deployment)

```bash
# 1. configure
cp .env.example .env
$EDITOR .env    # fill in at minimum: AZURE_TENANT_ID, MCP_RESOURCE_IDENTIFIER,
                #   MCP_PUBLIC_BASE_URL (see "What every config value does" below)

# 2. run
docker compose up -d

# 3. verify — the discovery endpoint responds without authentication
curl -s http://localhost:8080/.well-known/oauth-protected-resource/mcp | jq
# → { "resource": "...", "authorization_servers": ["https://login.microsoftonline.com/..."] }
```

To build the image locally instead of pulling from GHCR, open `compose.yaml`,
comment out the `image:` line and uncomment `build: .`, then run
`docker compose up -d --build`.

### Option B — uv (local dev + cloudflared tunnel)

```bash
# prerequisites: Python 3.12, uv, cloudflared
brew install uv cloudflared

uv sync
cp .env.example .env
$EDITOR .env

# starts server + public HTTPS tunnel; wires tunnel URL into .well-known metadata
make serve
```

`make serve` prints the public tunnel URL and the `/mcp` endpoint to paste into
Blockbrain. Ctrl+C shuts both processes cleanly.

For local testing without a tunnel (e.g. with `az account get-access-token`),
use `make dev` instead.

---

## Setting up the identity provider

A full step-by-step for each supported provider lives in
[`docs/PROVIDERS.md`](docs/PROVIDERS.md). The Entra ID version (most common):

### Azure Portal — one-time setup per MCP server

1. **App registrations → New registration**
   - Name: `Blockbrain MCP — <your-server-name>`
   - Supported account types: **Accounts in this organizational directory only**
     (Single-tenant) — the 95% case
   - Redirect URI: **leave blank for now**, you will fill it in after
     creating the MCP-server record in Blockbrain
2. Copy from the Overview page:
   - **Directory (tenant) ID** → `.env` → `AZURE_TENANT_ID`
   - **Application (client) ID** → save for Blockbrain UI
3. **Manifest** — set `api.requestedAccessTokenVersion: 2` and save.
   This flips the app from legacy v1 tokens (`iss: sts.windows.net`) to
   v2 tokens (`iss: login.microsoftonline.com/<tid>/v2.0`), which is what
   this template validates against.
4. **Expose an API**
   - Set **Application ID URI** to `api://<application-client-id>` (accept the
     default) → `.env` → `MCP_RESOURCE_IDENTIFIER`
   - **Add a scope**: name `mcp.access`, Admin + user consent allowed,
     display name "Call MCP tools" → `.env` → `MCP_REQUIRED_SCOPES=mcp.access`
5. **Certificates & secrets → New client secret**
   - Save the **Value** (only shown once) for the Blockbrain UI
6. **Authentication → Platform configurations → Add a platform → Web**
   - Redirect URI = the callback URL Blockbrain shows in the UI after you
     create the MCP-server record (see next section)

### In Blockbrain

1. **MCP-Server → New → Auth type: OAuth 2.1**
2. Enter the server URL (the `/mcp` endpoint from `make tunnel` or your prod URL)
3. Blockbrain calls `GET /.well-known/oauth-protected-resource/mcp` on your
   server (Tier 1 discovery) and shows you the callback URL for Azure.
   Format: `https://integrations.theblockbrain.ai/api/v1/mcp-servers/<configId>/oauth/callback`
4. Copy that URL into Azure (step 5 above)
5. Paste **Application (client) ID** and **Client secret** into the
   Blockbrain form (Blockbrain encrypts both with AES-256-GCM)
6. User clicks **Connect** → redirected to Microsoft login → consent →
   token is stored → Blockbrain calls your tools with
   `Authorization: Bearer <entra-jwt>`

---

## What every config value does

All variables can be set in `.env` (copied from `.env.example`). The
`compose.yaml` file mirrors this table with inline comments.

| Env var                       | Required?   | Default                    | What it is                                                                                    |
|-------------------------------|-------------|----------------------------|-----------------------------------------------------------------------------------------------|
| `AZURE_TENANT_ID`             | either/or   | —                          | Tenant UUID for Entra single-tenant; or `common` for multi-tenant (path A/B)                 |
| `AZURE_ALLOWED_TENANTS`       | if common   | —                          | Comma-separated tenant UUIDs allowed when `AZURE_TENANT_ID=common`                           |
| `OIDC_ISSUER_URL`             | either/or   | —                          | Issuer URL for non-Entra providers (Zitadel, Auth0, Okta, Keycloak, …) — path C             |
| `OIDC_JWKS_URI`               | optional    | derived from issuer        | Override JWKS endpoint. Defaults to `<issuer>/.well-known/jwks.json`                         |
| `MCP_RESOURCE_IDENTIFIER`     | required    | —                          | Value that must appear in the JWT `aud`. For Entra: the Application ID URI (`api://<id>`)    |
| `MCP_PUBLIC_BASE_URL`         | conditional | —                          | Required when `MCP_RESOURCE_IDENTIFIER` is not an http(s) URL (Entra case). Public server URL |
| `MCP_REQUIRED_SCOPES`         | optional    | (none)                     | Comma-separated scopes every token must carry (e.g. `mcp.access`). Published in the RFC 9728 Protected Resource Metadata |
| `MCP_ADVERTISE_SCOPES_IN_AS_METADATA` | optional | `true`              | Also copy the scopes into the proxied authorization server metadata, for clients that ignore Protected Resource Metadata |
| `MCP_ALLOW_DEFAULT_SCOPE`     | optional    | `false`                    | Advertise `<resource>/.default` when no scopes are pinned. Entra then bundles every admin-consented scope, so pin scopes instead |
| `MCP_SERVER_NAME`             | optional    | `Blockbrain MCP Template`  | Human-readable name advertised in MCP metadata                                                |
| `MCP_HOST`                    | optional    | `0.0.0.0`                  | Bind address (keep `0.0.0.0` in Docker)                                                       |
| `MCP_PORT`                    | optional    | `8080`                     | Port the server listens on                                                                     |
| `MCP_LOG_LEVEL`               | optional    | `INFO`                     | `DEBUG` / `INFO` / `WARNING` / `ERROR`                                                        |
| `MCP_AUTH_DISABLED`           | optional    | `false`                    | **DANGER** — disables OAuth/JWT auth entirely. Local Inspector testing only, never deployed   |
| `BRAVE_API_KEY`               | optional    | (none)                     | Brave Search API key — only needed for `web_search` / `research_topic` tools                 |
| `RATE_LIMIT_RESEARCH_PER_DAY` | optional    | `50`                       | Daily per-user cap for the `research_topic` tool                                              |
| `READ_URL_ALLOWED_DOMAINS`    | optional    | (any public host)          | Comma-separated domain allow-list for `read_url` / `research_topic`. Subdomains included      |
| `READ_URL_MAX_BYTES`          | optional    | `2000000`                  | Byte cap on a fetched response body                                                           |
| `READ_URL_MAX_CHARS`          | optional    | `100000`                   | Character cap on the Markdown handed back to the model                                        |

---

## How the OAuth 2.1 flow actually works end-to-end

```
 Customer                 Blockbrain                 MCP Server                 Entra ID (IdP)
    │                         │                          │                           │
    │  1. URL + OAuth 2.1     │                          │                           │
    ├────────────────────────▶│                          │                           │
    │                         │  2. GET /.well-known/…   │                           │
    │                         ├─────────────────────────▶│                           │
    │                         │  {authorization_servers: [entra-tenant-issuer]}      │
    │                         │◀─────────────────────────┤                           │
    │                         │                                                      │
    │                         │  3. GET /.well-known/openid-configuration            │
    │                         ├─────────────────────────────────────────────────────▶│
    │  4. fill client_id/sec  │                                                      │
    ├────────────────────────▶│                                                      │
    │                         │                                                      │
    │  5. Connect             │                                                      │
    ├────────────────────────▶│  6. /authorize + PKCE                                │
    │                         ├─────────────────────────────────────────────────────▶│
    │    user logs in + consents                                                     │
    │◀───────────────────────────────────────────────────────────────────────────────┤
    │                         │  7. code → token                                     │
    │                         ├─────────────────────────────────────────────────────▶│
    │                         │◀─────────────────────────────────────────────────────┤
    │                         │                                                      │
    │                         │  8. tool call + Bearer  │                            │
    │                         ├─────────────────────────▶│ verify(jwt, aud, iss, exp)│
    │                         │◀─────────────────────────┤                           │
```

Steps 2–3 happen once per MCP-server registration. Step 8 happens on every
tool call. Tokens are auto-refreshed by Blockbrain within a 5-minute buffer
before expiry (see the Blockbrain OAuth service).

---

## Project layout

```
blockbrain-mcp/
├── src/blockbrain_mcp/
│   ├── server.py          # main entry; assembles FastMCP
│   ├── config.py          # ⭐ CUSTOMIZE HERE for new env vars
│   ├── auth.py            # JWTVerifier + RemoteAuthProvider
│   ├── context.py         # JWT/header → UserContext (Entra/Zitadel/OIDC claims)
│   ├── logging_setup.py   # structlog JSON
│   └── tools/
│       ├── __init__.py    # ⭐ CUSTOMIZE HERE — tool registry
│       ├── _base.py
│       ├── whoami.py      # keep — demonstrates OAuth context
│       ├── web_search.py  # ⭐ REPLACEABLE
│       ├── read_url.py    # ⭐ REPLACEABLE
│       └── research.py    # ⭐ REPLACEABLE — example composite
├── tests/                 # pytest, 23 tests
├── docs/
│   ├── ARCHITECTURE.md    # deeper design dive
│   ├── PROVIDERS.md       # Entra / Zitadel / Auth0 / Okta / Keycloak setup
│   └── ADDING_A_TOOL.md   # step-by-step for extending the server
├── compose.yaml           # Docker Compose with full env-var reference
├── Dockerfile             # multi-stage build (uv builder → slim runtime)
├── .env.example           # all config vars with comments
├── scripts/dev-tunnel.sh  # cloudflared tunnel helper
├── pyproject.toml
└── Makefile
```

See [`docs/ADDING_A_TOOL.md`](docs/ADDING_A_TOOL.md) for the three-step guide
to adding your own tool.

---

## Local verification without an Identity Provider round-trip

```bash
# With a configured .env:
make dev &                                     # starts the server
curl -s http://localhost:8080/.well-known/oauth-protected-resource/mcp | jq
# → JSON listing your Entra tenant's issuer as authorization_servers

# Unauthenticated call — must return 401 with WWW-Authenticate header
curl -s -i -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

For a full-stack test with a real Entra token, issue one from your tenant
(e.g. via `az account get-access-token --resource api://blockbrain-mcp`) and
`Authorization: Bearer $TOKEN` the request.

---

## Local testing without auth (MCP Inspector)

For quick local iteration you don't need Azure/Entra, a tunnel, or any real
tokens at all — set `MCP_AUTH_DISABLED=true` and point the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector) straight
at your local server:

```bash
cp .env.example .env
# in .env: MCP_AUTH_DISABLED=true, MCP_HOST=127.0.0.1
uv run blockbrain-mcp
# in another terminal:
npx @modelcontextprotocol/inspector
# → connect to http://localhost:8080/mcp, no auth needed
```

**This is for local testing only.** With auth disabled, `/mcp` accepts
*any* request, no token required — the server prints a loud warning banner
on startup, and an even louder one if `MCP_HOST` is not a localhost address
(the default `0.0.0.0` included). Never set `MCP_AUTH_DISABLED=true` in a
deployed or network-reachable environment. Leave it unset (or `false`) for
everything else — that is the default and enforces auth exactly like before
this flag existed.

Tools that resolve the caller's identity (`whoami`, `web_search`, `read_url`,
`research_topic`) still call `get_user_context()`. With auth disabled and no
real bearer token present, that returns a synthetic **dev identity**
(`user_id="local-dev"`, `email="dev@localhost"`, `name="Local Dev User
(MCP_AUTH_DISABLED)"`) instead of raising — the values are deliberately
obvious placeholders so they can never be mistaken for a real user in logs or
downstream code. This lets the Inspector actually *call* every tool locally,
not just list them. If a real token does show up while auth is disabled, its
claims are used as-is; the dev identity is only a fallback for when nothing
identifies the caller. The fallback is gated strictly on
`MCP_AUTH_DISABLED=true` — with auth enabled (the default), a missing or
unidentifiable token still raises `NotAuthenticatedError` exactly as before.

---

## Tests & linting

```bash
make test    # uv run pytest  (config paths, auth wiring, claims, fetch guard, tools)
make lint    # uv run ruff check + format --check
make fmt     # uv run ruff format + --fix
```

---

## Security posture

- Bearer-token enforcement on every tool call (FastMCP middleware)
- Audience validation per the MCP spec (Resource Indicators, RFC 8707)
- **Identity and org come from verified token claims only.** The
  `X-Blockbrain-*` request headers are set by the caller and are never read.
  `org_id` is the tenant boundary: a token without an org claim yields
  `org_id=None`, and `require_org_id()` refuses rather than treating the
  request as unscoped. If your provider does not emit an org or e-mail claim,
  add it in the provider's scope and claim configuration
- **Outbound fetches are guarded.** `read_url` and `research_topic` accept
  https only, reject private, loopback, link-local and other reserved address
  ranges, re-check every redirect hop, cap the response size, and cap the text
  handed to the model. Set `READ_URL_ALLOWED_DOMAINS` when you know the target
  hosts. A host can still change what it resolves to between the check and the
  connection (DNS rebinding), which the allow-list closes off
- Page content a tool fetches is untrusted input that reaches the model's
  context. Treat it as data, never as instructions
- **Logs are redacted and correlated.** Secrets are dropped, e-mail local parts
  are masked, URL query strings are stripped, and every line carries a
  `request_id` taken from the incoming `traceparent` or `X-Request-Id`
- `compose.yaml` pins a released image tag. Do not track `:latest`: read the
  changelog, then bump the tag deliberately
- CI runs ruff, pytest and a dependency audit before any image is built, and
  scans the published image. Dependabot keeps the lockfile current
- Rate limits in `research_topic` are in-memory for this template. Use Redis
  for production
- `MCP_AUTH_DISABLED` is `false` by default — auth must be explicitly opted
  out of, never explicitly opted into. It's meant for local MCP Inspector
  testing only and logs a loud startup warning (louder still if the server
  isn't bound to localhost) so it can't be silently left on

---

## References

- MCP Authorization Spec (2026-07-28):
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- Why `/.well-known/oauth-authorization-server` deviates from RFC 8414 §3.3:
  [docs/adr/2026-08-18_well-known-authorization-server-proxy.md](docs/adr/2026-08-18_well-known-authorization-server-proxy.md)
- FastMCP v3 docs: https://gofastmcp.com
- Entra ID token reference:
  https://learn.microsoft.com/entra/identity-platform/access-token-claims-reference
- RFC 9728 — OAuth 2.0 Protected Resource Metadata
- RFC 8414 — OAuth 2.0 Authorization Server Metadata
- RFC 7591 — OAuth 2.0 Dynamic Client Registration
- RFC 7636 — PKCE
- RFC 8707 — Resource Indicators
