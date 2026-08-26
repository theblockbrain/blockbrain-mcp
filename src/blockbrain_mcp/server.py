"""
Main entry point. Assembles the FastMCP server from config + auth + tools.

You should not need to edit this file. To add a tool, edit `tools/__init__.py`.
To change auth, edit `auth.py`. To change env vars, edit `config.py`.
"""

import sys

import structlog
from fastmcp import FastMCP
from starlette.middleware import Middleware

from .auth import build_auth_provider
from .config import get_settings
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
  That would serve a fully unauthenticated MCP endpoint reachable from outside
  this machine. Set MCP_HOST=127.0.0.1 for local Inspector testing, or
  re-enable auth.
################################################################################
"""


class UnsafeAuthDisabledConfigError(RuntimeError):
    """Raised when MCP_AUTH_DISABLED=true is combined with a non-localhost MCP_HOST."""


def build_server() -> FastMCP:
    configure_logging()
    settings = get_settings()

    if settings.mcp_auth_disabled:
        if settings.mcp_host not in _LOCALHOST_ADDRESSES:
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

        print(_AUTH_DISABLED_BANNER, file=sys.stderr)
        log.warning("auth.disabled", reason="MCP_AUTH_DISABLED=true — local testing only")

        mcp: FastMCP = FastMCP(name=settings.mcp_server_name)
    else:
        mcp = FastMCP(
            name=settings.mcp_server_name,
            auth=build_auth_provider(),
        )

    for tool_fn in ALL_TOOLS:
        mcp.tool(tool_fn)

    if not settings.mcp_auth_disabled:
        register_well_known_routes(mcp)
    return mcp


def main() -> None:
    settings = get_settings()
    mcp = build_server()
    mcp.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path="/mcp",
        middleware=[Middleware(CorrelationIdMiddleware)],
    )


if __name__ == "__main__":
    main()
