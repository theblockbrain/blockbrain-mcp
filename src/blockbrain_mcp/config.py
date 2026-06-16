"""
Typed environment-variable loading.

⭐ CUSTOMIZE HERE — add your own settings below and document them in
   `.env.example`.

Provider configuration — pick ONE of these three paths:

  A) Entra ID (Azure AD) — Single-Tenant [RECOMMENDED DEFAULT]
         AZURE_TENANT_ID=<tenant-uuid>

  B) Entra ID (Azure AD) — Multi-Tenant with explicit allow-list
         AZURE_TENANT_ID=common
         AZURE_ALLOWED_TENANTS=<tenant-uuid-a>,<tenant-uuid-b>

  C) Any other OIDC provider (Zitadel, Auth0, Okta, Keycloak, ...)
         OIDC_ISSUER_URL=https://auth.example.com
         OIDC_JWKS_URI=https://auth.example.com/.well-known/jwks.json  # optional if derivable

Always required:
         MCP_RESOURCE_IDENTIFIER=<your server audience>   # e.g. api://blockbrain-mcp
"""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENTRA_AUTHORITY = "https://login.microsoftonline.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------- Provider: Entra ID shortcuts ---------------------------------
    azure_tenant_id: str = Field(
        default="",
        description=(
            "Directory (tenant) ID from Azure Portal. Setting this is the "
            "quickest path: issuer and JWKS are derived automatically. "
            "Use 'common' together with AZURE_ALLOWED_TENANTS for multi-tenant."
        ),
    )
    azure_allowed_tenants: str = Field(
        default="",
        description=(
            "Comma-separated list of Azure tenant UUIDs that are allowed to "
            "authenticate. Only used when AZURE_TENANT_ID='common'."
        ),
    )

    # -------- Provider: generic OIDC ---------------------------------------
    oidc_issuer_url: str = Field(
        default="",
        description=(
            "OIDC issuer URL (e.g. https://auth.example.com). Leave blank "
            "when AZURE_TENANT_ID is set."
        ),
    )
    oidc_jwks_uri: str = Field(
        default="",
        description=(
            "JWKS URI. Defaults to `<issuer>/.well-known/jwks.json` for "
            "generic OIDC, or to Azure's JWKS for Entra."
        ),
    )

    # -------- Always required ----------------------------------------------
    mcp_resource_identifier: str = Field(
        ...,
        description=(
            "Must match the `aud` claim in issued tokens. For Entra: the "
            "Application ID URI from 'Expose an API' (e.g. api://<app-id>). "
            "For other providers: usually an https URL identifying the server."
        ),
    )
    mcp_required_scopes: str = Field(
        default="",
        description="Comma-separated list of required OAuth scopes.",
    )

    # -------- Server -------------------------------------------------------
    mcp_server_name: str = Field(default="Blockbrain MCP Template")
    mcp_host: str = Field(default="0.0.0.0")
    mcp_port: int = Field(default=8080)
    mcp_log_level: str = Field(default="INFO")
    mcp_public_base_url: str = Field(
        default="",
        description="Public URL Blockbrain uses to reach this server. Falls back to mcp_resource_identifier.",
    )

    # -------- Tool-specific (⭐ CUSTOMIZE HERE) ----------------------------
    brave_api_key: str = Field(default="", description="Brave Search API key.")
    rate_limit_research_per_day: int = Field(default=50)

    # ---------------------------------------------------------------------
    # Derived helpers
    # ---------------------------------------------------------------------

    @property
    def is_entra(self) -> bool:
        return bool(self.azure_tenant_id)

    @property
    def allowed_tenants(self) -> list[str]:
        return [t.strip() for t in self.azure_allowed_tenants.split(",") if t.strip()]

    @property
    def required_scopes(self) -> list[str]:
        return [s.strip() for s in self.mcp_required_scopes.split(",") if s.strip()]

    @property
    def audience(self) -> str | list[str]:
        """Allowed `aud` values on incoming tokens.

        Entra v1 tokens carry the full Application ID URI (`api://<guid>`),
        v2 tokens carry the bare Application (client) ID GUID. We accept
        whichever the user configured AND — when the configured value is
        `api://<guid>` — the bare GUID too, so the template works with v1,
        v2, and mixed setups without config changes.
        """
        primary = self.mcp_resource_identifier.rstrip("/")
        if primary.startswith("api://"):
            bare = primary[len("api://") :]
            return [primary, bare]
        return primary

    @property
    def public_base_url(self) -> str:
        if self.mcp_public_base_url:
            return self.mcp_public_base_url.rstrip("/")
        # Audience is only a sensible fallback when it IS an http(s) URL.
        # For Entra-style audiences the user must set MCP_PUBLIC_BASE_URL.
        raw = self.mcp_resource_identifier.rstrip("/")
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        raise ValueError(
            "MCP_PUBLIC_BASE_URL is required because MCP_RESOURCE_IDENTIFIER "
            "is not an http(s) URL. Set it to the public URL of this server "
            "(e.g. your cloudflared tunnel URL during local dev)."
        )

    # The issuer can be either a single string (single-tenant / generic OIDC)
    # or a list of strings (Entra multi-tenant with allow-list).
    @property
    def issuer(self) -> str | list[str]:
        if self.is_entra:
            if self.azure_tenant_id.lower() == "common":
                if not self.allowed_tenants:
                    raise ValueError(
                        "AZURE_TENANT_ID='common' requires AZURE_ALLOWED_TENANTS "
                        "to be set (comma-separated tenant UUIDs)."
                    )
                return [f"{ENTRA_AUTHORITY}/{t}/v2.0" for t in self.allowed_tenants]
            return f"{ENTRA_AUTHORITY}/{self.azure_tenant_id}/v2.0"
        return self.oidc_issuer_url.rstrip("/")

    @property
    def jwks_uri(self) -> str:
        if self.oidc_jwks_uri:
            return self.oidc_jwks_uri
        if self.is_entra:
            tenant = self.azure_tenant_id if self.azure_tenant_id.lower() != "common" else "common"
            return f"{ENTRA_AUTHORITY}/{tenant}/discovery/v2.0/keys"
        if self.oidc_issuer_url:
            return f"{self.oidc_issuer_url.rstrip('/')}/.well-known/jwks.json"
        raise ValueError("Cannot derive jwks_uri — set AZURE_TENANT_ID or OIDC_ISSUER_URL.")

    # Pick ONE primary-issuer URL for the .well-known metadata document.
    # RFC 9728 allows multiple `authorization_servers`; we list all of them.
    @property
    def authorization_servers(self) -> list[str]:
        iss = self.issuer
        return iss if isinstance(iss, list) else [iss]

    # Upstream OpenID Connect Discovery document URL.
    # Used by our own /.well-known/oauth-authorization-server proxy route,
    # which works around MCP clients (like Blockbrain) that look for RFC 8414
    # metadata at the path-inserted location — where Entra ID does not serve
    # it. Entra only serves the OIDC form (path-appended openid-configuration).
    @property
    def openid_configuration_urls(self) -> list[str]:
        def _mk(issuer: str) -> str:
            return f"{issuer.rstrip('/')}/.well-known/openid-configuration"

        iss = self.issuer
        return [_mk(u) for u in (iss if isinstance(iss, list) else [iss])]

    @model_validator(mode="after")
    def _exactly_one_provider(self) -> "Settings":
        if self.is_entra and self.oidc_issuer_url:
            raise ValueError(
                "Set EITHER AZURE_TENANT_ID (for Entra) OR OIDC_ISSUER_URL "
                "(for other providers), not both."
            )
        if not self.is_entra and not self.oidc_issuer_url:
            raise ValueError(
                "No provider configured. Set AZURE_TENANT_ID (Entra) or "
                "OIDC_ISSUER_URL (other OIDC provider)."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
