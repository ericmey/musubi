"""FastMCP server setup and entry points."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from musubi.adapters.mcp.tools import attach_tools
from musubi.sdk.async_client import AsyncMusubiClient

logger = logging.getLogger(__name__)


def create_mcp_server(client: AsyncMusubiClient) -> FastMCP:
    """Create and configure the FastMCP server."""
    mcp = FastMCP("musubi")
    attach_tools(mcp, client)
    return mcp


def run_stdio() -> None:
    """Run the MCP server over stdio.

    Expects MUSUBI_API_URL and MUSUBI_TOKEN in the environment.
    """
    api_url = os.environ.get("MUSUBI_API_URL", "http://localhost:8000/v1")
    token = os.environ.get("MUSUBI_TOKEN", "")

    if not token:
        logger.warning(
            "No MUSUBI_TOKEN found in environment; auth will fail if server requires it."
        )

    client = AsyncMusubiClient(base_url=api_url, token=token)
    mcp = create_mcp_server(client)

    # We must run this synchronously since mcp.run() handles its own event loop
    mcp.run()


def run_sse() -> None:
    """Run the MCP server over SSE via Starlette/FastAPI.

    This is intended to be mounted or run via uvicorn in a production deployment
    behind an OAuth 2.1 reverse proxy.
    """
    # The ASGI app can be extracted from mcp via mcp.create_app() but FastMCP
    # run() doesn't return the app directly if we want to run uvicorn.
    # For this adapter, we just provide the basic structure.

    api_url = os.environ.get("MUSUBI_API_URL", "http://localhost:8000/v1")
    token = os.environ.get("MUSUBI_TOKEN", "")

    client = AsyncMusubiClient(base_url=api_url, token=token)
    mcp = create_mcp_server(client)

    # Normally we'd use mcp.run() with transport="sse", which starts uvicorn internally.
    # FastMCP defaults to stdio if not specified, so we specify transport.
    mcp.run(transport="sse")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sse":
        run_sse()
    else:
        run_stdio()
