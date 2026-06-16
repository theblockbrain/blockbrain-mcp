# Identity-provider setup

This template is generic OIDC. Pick your provider, follow the per-provider
steps below, set the env vars, and you are done. The `auth.py` code does not
change.

All providers need **two pieces of configuration** on the MCP-server side:

| env var                   | what it is                                                    |
|---------------------------|---------------------------------------------------------------|
| `…_ISSUER_URL`            | the `iss` the provider puts in JWTs                           |
| `MCP_RESOURCE_IDENTIFIER` | the `aud` the provider puts in JWTs — configured on THE APP   |

Plus `MCP_PUBLIC_BASE_URL` when `MCP_RESOURCE_IDENTIFIER` is not an http(s) URL.

On the Blockbrain side you always need **client-ID + client-secret** from the
provider, pasted into the Blockbrain UI. Blockbrain uses those to run the
standard OAuth 2.1 authorization-code + PKCE flow against the provider.

---

## Entra ID (Azure AD) — default

### Single-tenant (95% case)

1. **Azure Portal → App registrations → New registration**
   - Supported account types: **Accounts in this organizational directory only**
2. **Overview** — copy **Directory (tenant) ID** and **Application (client) ID**
3. **Manifest** — set `api.requestedAccessTokenVersion: 2`
   (Entra default is v1 which uses legacy `sts.windows.net` as issuer and
   ships audiences in URI form. v2 uses `login.microsoftonline.com/<tid>/v2.0`
   and ships audiences as the bare GUID — which is what this template expects.)
4. **Expose an API**
   - Application ID URI: `api://<application-client-id>`
   - Add a scope: `mcp.access`, "Admins and users"
5. **Expose an API → Authorized client applications** (only needed if you
   want to test locally via `az account get-access-token`)
   - Add Client ID `04b07795-8ddb-461a-bbee-02f9e1bf7b46` (Azure CLI) with the
     `mcp.access` scope ticked. Not needed for the Blockbrain integration.
6. **Certificates & secrets → New client secret** — copy the Value now
7. **Authentication → Add a platform → Web → Redirect URI**
   - Paste the callback URL Blockbrain shows after you create the MCP-server
     record (`https://integrations.<env>.theblockbrain.ai/api/v1/mcp-servers/<configId>/oauth/callback`)

```env
AZURE_TENANT_ID=<directory-tenant-id>
MCP_RESOURCE_IDENTIFIER=api://<application-client-id>
MCP_PUBLIC_BASE_URL=https://<your-public-tunnel-or-prod-hostname>
MCP_REQUIRED_SCOPES=mcp.access
```

The template accepts `api://<guid>` as well as the bare `<guid>` — both
Entra token versions (v1 and v2) will validate against either form.

### Multi-tenant (with allow-list)

Same steps as single-tenant, but in step 1 pick **Accounts in any
organizational directory**. Then:

```env
AZURE_TENANT_ID=common
AZURE_ALLOWED_TENANTS=<tenant-a-uuid>,<tenant-b-uuid>
MCP_RESOURCE_IDENTIFIER=api://<application-client-id>
MCP_PUBLIC_BASE_URL=https://<your-public-hostname>
MCP_REQUIRED_SCOPES=mcp.access
```

The server validates `iss` against the explicit list — tokens from unknown
tenants are rejected even though Azure would issue them.

Trade-offs of multi-tenant: larger attack surface, admin-consent friction
for first-login of new tenants, `email` claim not guaranteed (rely on
`oid`+`tid`). Only turn it on if you actually have a cross-tenant use case.

---

## Zitadel

1. In Zitadel Console: **Projects → New Project → Create Application → API**
2. Auth method: **JWT**, Client-Type: **API**
3. Copy **Client ID**, **Client Secret**
4. Under **URLs**, note the issuer (e.g. `https://auth.example.com`)
5. Define the **audience** — usually the Client ID itself, or a custom claim
   via an Action if you want a readable audience string

```env
OIDC_ISSUER_URL=https://auth.example.com
MCP_RESOURCE_IDENTIFIER=<client-id-or-custom-audience>
# MCP_PUBLIC_BASE_URL only needed if the audience isn't an https URL
```

Zitadel supports **RFC 7591 Dynamic Client Registration** in newer versions —
enable it on the instance to let Blockbrain self-register.

---

## Auth0

1. **Auth0 Dashboard → Applications → Create Application → Regular Web App**
2. Note **Domain** (= issuer, with `https://` prefix and trailing `/`)
3. **APIs → Create API**, identifier = `https://mcp.example.com` (that's your audience)
4. Back on the Application: allow callback URL (the Blockbrain callback) and
   enable the API under **APIs** tab

```env
OIDC_ISSUER_URL=https://<your-tenant>.auth0.com/
MCP_RESOURCE_IDENTIFIER=https://mcp.example.com
```

Auth0 does **not** support DCR for regular apps — paste Client ID + Secret
into Blockbrain manually.

---

## Okta

1. **Okta Admin → Applications → Create App Integration → OIDC → Web Application**
2. Sign-in redirect URI: the Blockbrain callback URL
3. **Security → API → Authorization Servers** → pick an auth server (or the default)
4. Note the **Issuer** URI (e.g. `https://<tenant>.okta.com/oauth2/default`)
5. Add a custom scope `mcp.access` on that auth server
6. Copy Client ID + Client Secret

```env
OIDC_ISSUER_URL=https://<tenant>.okta.com/oauth2/default
MCP_RESOURCE_IDENTIFIER=<audience you configured on the auth server>
MCP_REQUIRED_SCOPES=mcp.access
```

---

## Keycloak

1. In your realm: **Clients → Create client**
   - Client type: **OpenID Connect**
   - Client authentication: **On** (confidential)
2. **Settings → Valid redirect URIs**: Blockbrain callback URL
3. **Credentials tab**: copy the Client Secret
4. **Client scopes → create** `mcp.access` and add to the client as default
5. **Audience mapper**: Clients → your client → Client scopes → *-dedicated →
   Add mapper → Audience → set Included Client Audience to the client ID

```env
OIDC_ISSUER_URL=https://<keycloak-host>/realms/<realm-name>
MCP_RESOURCE_IDENTIFIER=<client-id>
MCP_REQUIRED_SCOPES=mcp.access
```

Keycloak **does** support RFC 7591 DCR — enable it on the realm to let
Blockbrain self-register and skip the manual client-secret step.

---

## Testing your setup locally

Once `.env` is filled in:

```bash
make dev
curl -s http://localhost:8080/.well-known/oauth-protected-resource/mcp | jq
```

The `authorization_servers` list in the response should match the issuer URL
you configured. If it does, Blockbrain's Tier 1 discovery will work.

Then get an access token from your provider (provider-specific — for Entra
use `az account get-access-token --resource <your-MCP_RESOURCE_IDENTIFIER>`;
for Zitadel use `scripts/get_zitadel_token.sh`) and make a real tool call:

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}' | jq
```

`whoami` should return your identity (email + org/tenant ID).
