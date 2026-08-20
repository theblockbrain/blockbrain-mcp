# Proxy the authorization server metadata, and keep scopes in Protected Resource Metadata

Date: 2026-08-18
Status: Accepted

## Context

MCP clients, Blockbrain's integrations service among them, discover an OAuth
authorization server by fetching the RFC 8414 §3 path-inserted URL:

```
https://<host>/.well-known/oauth-authorization-server<path>
```

Entra ID does not serve that URL. It publishes only the path-appended OpenID
Connect form, `<issuer>/.well-known/openid-configuration`, and returns 404 for
the RFC 8414 shape. Verified against a live tenant. A customer on Entra
therefore cannot complete client-side discovery against this server without
help, which blocks the whole OAuth flow.

Two further questions came out of the same review:

1. Where should the resource scopes be advertised?
2. What should happen when no scopes are configured?

## Decision

**1. We proxy the upstream OIDC discovery document.**

The server advertises itself as the authorization server in its RFC 9728
Protected Resource Metadata and serves the upstream document unchanged at
`/.well-known/oauth-authorization-server`.

This is a deliberate deviation from RFC 8414 §3.3, which requires the `issuer`
value inside the document to match the host the client fetched it from. Ours
does not: the document carries Entra's issuer while the client fetched it from
this server. Lenient clients (Blockbrain today) accept it. A strict client will
reject the document, and for that client the proxy has to be turned off and the
IdP's own metadata URL configured directly instead. Nothing about token
issuance or validation changes either way, because tokens stay signed by and
validated against the upstream IdP.

**2. Scopes live in Protected Resource Metadata.**

`scopes_supported` in our own RFC 9728 document is the field that exists for
telling a client which scopes to ask for, so that is where the resource scopes
go. `auth.py` populates it from `MCP_REQUIRED_SCOPES`.

We keep injecting the same scopes into the proxied authorization server
document, now behind `MCP_ADVERTISE_SCOPES_IN_AS_METADATA`, on by default. A
client that reads scopes only from that document would otherwise request a
token without the resource scope, and that token fails audience validation
here. The document is already a deliberate spec deviation, so leaving the shim
on costs nothing a stricter default would buy back, and it keeps every existing
deployment working. `MCP_ADVERTISE_SCOPES_IN_AS_METADATA=false` drops it once
the clients in play are known to read Protected Resource Metadata.

**3. `.default` is opt-in, not the fallback.**

With no scopes configured we previously advertised `<resource>/.default`, which
asks Entra to bundle every admin-consented scope for the resource. That is the
omnibus-scope pattern our own scope-minimization guidance warns about. The
default is now to advertise no resource scope at all, and
`MCP_ALLOW_DEFAULT_SCOPE=true` restores the old behaviour as a documented
escape hatch.

## Consequences

- Entra customers keep a working discovery path with no per-client patching.
- A strict RFC 8414 client will reject the proxied document. That is a known
  limitation, written down here rather than discovered during an integration.
- Scopes now appear in both documents, so no client loses a discovery path it
  had before. Only deployments that relied on the `.default` fallback change
  behaviour, and `MCP_ALLOW_DEFAULT_SCOPE=true` restores it.
- Pinning `MCP_REQUIRED_SCOPES` is now the path of least resistance, which is
  the behaviour we want.

## References

- RFC 8414 §3.3 (Authorization Server Metadata, issuer validation)
- RFC 9728 (OAuth 2.0 Protected Resource Metadata)
