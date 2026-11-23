"""
Main entry point. Assembles the FastMCP server from config + auth + tools.

You should not need to edit this file. To add a tool, edit `tools/__init__.py`.
To change auth, edit `auth.py`. To change env vars, edit `config.py`.
"""

import sys
from importlib.metadata import PackageNotFoundError, version

import structlog
from fastmcp import FastMCP
from starlette.middleware import Middleware

from .auth import build_auth_provider
from .config import Settings, get_settings
from .logging_setup import CorrelationIdMiddleware, configure_logging
from .tools import ALL_TOOLS
from .well_known import register_well_known_routes

log = structlog.get_logger(__name__)

# Bind addresses that stay on the local machine. Anything else (including the
# default `0.0.0.0`, which listens on every interface) means "reachable from
# the network" and gets the extra, more urgent warning below.
_LOCALHOST_ADDRESSES = {"127.0.0.1", "localhost", "::1"}

_AUTH_DISABLED_BANNER = """
================================================================================
  WARNING: MCP_AUTH_DISABLED=true -- OAuth/JWT authentication is OFF.
  The /mcp endpoint accepts UNAUTHENTICATED requests.
  This is for LOCAL TESTING with the MCP Inspector ONLY.
  NEVER run this exposed to a network or in production.
================================================================================
"""

_AUTH_DISABLED_EXPOSED_BANNER = """
################################################################################
  REFUSING TO START: MCP_AUTH_DISABLED=true AND MCP_HOST=%s (not localhost-only).
  That would bind a fully unauthenticated MCP endpoint to a network-facing
  address. Set MCP_HOST=127.0.0.1 for local Inspector testing, or re-enable
  auth. Note this only checks the bind address -- a tunnel or port-forward
  pointed at that same localhost port is still reachable from outside this
  machine.
################################################################################
"""


class UnsafeAuthDisabledConfigError(RuntimeError):
    """Raised when MCP_AUTH_DISABLED=true is combined with a non-localhost MCP_HOST."""


def _refuse_unsafe_auth_disabled_bind(settings: Settings) -> None:
    """Fail closed before we ever bind a socket for an unauthenticated server.

    Lives in `main()` rather than `build_server()`: `build_server()` only
    assembles a `FastMCP` instance and never binds an address, so an embedder
    that builds the server and mounts `http_app()` on its own address (rather
    than calling `main()`) isn't hard-failed by a `MCP_HOST` value it doesn't
    even use.
    """
    if settings.mcp_auth_disabled and settings.mcp_host not in _LOCALHOST_ADDRESSES:
        print(_AUTH_DISABLED_EXPOSED_BANNER % settings.mcp_host, file=sys.stderr)
        log.error(
            "auth.disabled.refused_to_start",
            host=settings.mcp_host,
            reason="auth disabled while bound to a non-localhost address",
        )
        raise UnsafeAuthDisabledConfigError(
            f"MCP_AUTH_DISABLED=true and MCP_HOST={settings.mcp_host!r} is not "
            "localhost-only. Refusing to start a fully unauthenticated, "
            "network-reachable server. Set MCP_HOST=127.0.0.1 for local Inspector "
            "testing, or re-enable auth."
        )


def _server_version() -> str:
    try:
        return version("blockbrain-mcp")
    except PackageNotFoundError:  # running from a source tree that was never installed
        return "0.0.0"


def build_server() -> FastMCP:
    configure_logging()
    settings = get_settings()

    if settings.mcp_auth_disabled:
        print(_AUTH_DISABLED_BANNER, file=sys.stderr)
        log.warning("auth.disabled", reason="MCP_AUTH_DISABLED=true — local testing only")

    # `version` ends up in `serverInfo`, which 2026-07-28 clients read from
    # `server/discover` and from every result's `_meta`. Passing the tools to
    # the constructor (rather than `mcp.tool(fn)`) keeps the title and
    # annotations each tool declares with `@tool(...)` in its own module.
    mcp = FastMCP(
        name=settings.mcp_server_name,
        version=_server_version(),
        auth=None if settings.mcp_auth_disabled else build_auth_provider(),
        tools=ALL_TOOLS,
    )

    if not settings.mcp_auth_disabled:
        register_well_known_routes(mcp)
    return mcp


def main() -> None:
    settings = get_settings()
    _refuse_unsafe_auth_disabled_bind(settings)
    mcp = build_server()
    mcp.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path="/mcp",
        middleware=[Middleware(CorrelationIdMiddleware)],
        # The transport spec requires Origin validation (403 on a foreign
        # Origin) against DNS rebinding. "auto" enforces Host and Origin
        # checks whenever the server is reached over loopback, which is the
        # only way the unauthenticated MCP_AUTH_DISABLED mode can run. With
        # auth on, a rebinding page cannot attach the bearer token, and a
        # strict Host check would reject the public hostname a tunnel or
        # reverse proxy forwards, so it stays off there.
        host_origin_protection="auto" if settings.mcp_auth_disabled else False,
    )


if __name__ == "__main__":
    main()
