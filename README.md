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

## 60-second quickstart (Entra)

```bash
# 1. prerequisites: Python 3.12, uv, cloudflared
brew install uv cloudflared

# 2. install
uv sync

# 3. configure
cp .env.example .env
$EDITOR .env          # set AZURE_TENANT_ID, MCP_RESOURCE_IDENTIFIER, (optional) BRAVE_API_KEY

# 4. run — ONE command: starts server + public HTTPS tunnel and wires
#    the tunnel URL into the server's .well-known metadata automatically.
make serve
```

`make serve` prints a banner with the public tunnel URL and the `/mcp` endpoint
to paste into Blockbrain. Ctrl+C shuts both processes down cleanly.

If you just want the server without a tunnel (e.g. pure local testing with a
token from `az account get-access-token`), use `make dev` instead.

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
   Format: `https://integrations.<env>.theblockbrain.ai/api/v1/mcp-servers/<configId>/oauth/callback`
4. Copy that URL into Azure (step 5 above)
5. Paste **Application (client) ID** and **Client secret** into the
   Blockbrain form (Blockbrain encrypts both with AES-256-GCM)
6. User clicks **Connect** → redirected to Microsoft login → consent →
   token is stored → Blockbrain calls your tools with
   `Authorization: Bearer <entra-jwt>`

---

## What every config value does

| Env var                      | Required? | What it is                                                                                   |
|------------------------------|-----------|----------------------------------------------------------------------------------------------|
| `AZURE_TENANT_ID`            | either/or | Tenant UUID for Entra single-tenant; or `common` for multi-tenant                            |
| `AZURE_ALLOWED_TENANTS`      | if common | Comma-separated tenant UUIDs allowed when `AZURE_TENANT_ID=common`                           |
| `OIDC_ISSUER_URL`            | either/or | Issuer URL for non-Entra providers (Zitadel, Auth0, Okta, Keycloak, ...)                     |
| `OIDC_JWKS_URI`              | optional  | Override. Defaults to `<issuer>/.well-known/jwks.json` for generic OIDC                      |
| `MCP_RESOURCE_IDENTIFIER`    | required  | Value that must appear in the JWT's `aud`. For Entra: your Application ID URI                |
| `MCP_REQUIRED_SCOPES`        | optional  | Comma-separated scopes every call must carry                                                 |
| `MCP_PUBLIC_BASE_URL`        | conditional | Required when `MCP_RESOURCE_IDENTIFIER` isn't an http(s) URL (Entra case). Your public URL |
| `MCP_HOST` / `MCP_PORT`      | optional  | Bind address, default `0.0.0.0:8080`                                                         |
| `BRAVE_API_KEY`              | optional  | Only needed if you keep the `web_search` / `research_topic` tools                            |

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
├── scripts/dev-tunnel.sh          # cloudflared tunnel helper
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

## Tests & linting

```bash
make test    # uv run pytest  (23 tests: config paths, auth wiring, claims, tools)
make lint    # uv run ruff check + format --check
make fmt     # uv run ruff format + --fix
```

---

## Security posture

- Bearer-token enforcement on every tool call (FastMCP middleware)
- Audience validation per MCP 2025-06-18 spec (Resource Indicators, RFC 8707)
- `X-Blockbrain-*` headers are inputs, not trust roots — JWT claims always win
- Tokens are never logged
- Rate limits in `research_topic` are in-memory for this template; use Redis
  for production

---

## References

- MCP Authorization Spec (2025-06-18):
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- FastMCP v3 docs: https://gofastmcp.com
- Entra ID token reference:
  https://learn.microsoft.com/entra/identity-platform/access-token-claims-reference
- RFC 9728 — OAuth 2.0 Protected Resource Metadata
- RFC 8414 — OAuth 2.0 Authorization Server Metadata
- RFC 7591 — OAuth 2.0 Dynamic Client Registration
- RFC 7636 — PKCE
- RFC 8707 — Resource Indicators
