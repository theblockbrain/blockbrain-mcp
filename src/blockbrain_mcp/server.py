"""
Main entry point. Assembles the FastMCP server from config + auth + tools.

You should not need to edit this file. To add a tool, edit `tools/__init__.py`.
To change auth, edit `auth.py`. To change env vars, edit `config.py`.
"""

from fastmcp import FastMCP

from .auth import build_auth_provider
from .config import get_settings
from .logging_setup import configure_logging
from .tools import ALL_TOOLS
from .well_known import register_well_known_routes


def build_server() -> FastMCP:
    configure_logging()
    settings = get_settings()

    mcp: FastMCP = FastMCP(
        name=settings.mcp_server_name,
        auth=build_auth_provider(),
    )

    for tool_fn in ALL_TOOLS:
        mcp.tool(tool_fn)

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
    )


if __name__ == "__main__":
    main()
