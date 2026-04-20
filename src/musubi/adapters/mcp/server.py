"""FastMCP server setup and entry points."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from musubi.adapters.mcp.tools import attach_tools
from musubi.config import get_settings
from musubi.sdk.async_client import AsyncMusubiClient

logger = logging.getLogger(__name__)


def create_mcp_server(client: AsyncMusubiClient) -> FastMCP:
    """Create and configure the FastMCP server."""
    mcp = FastMCP("musubi")
    attach_tools(mcp, client)
    return mcp


def _build_client() -> AsyncMusubiClient:
    """Construct an SDK client from process-wide Settings.

    Reads ``MUSUBI_API_URL`` and ``MUSUBI_TOKEN`` via pydantic-settings at
    ``musubi.config`` — the single allowed env-read site per project
    guardrails. Non-MCP processes don't need to set these; defaults keep
    them safe.
    """
    settings = get_settings()
    api_url = str(settings.musubi_api_url)
    token = settings.musubi_token.get_secret_value()
    if not token:
        logger.warning("No MUSUBI_TOKEN configured; auth will fail if the API requires it.")
    return AsyncMusubiClient(base_url=api_url, token=token)


def run_stdio() -> None:
    """Run the MCP server over stdio."""
    client = _build_client()
    mcp = create_mcp_server(client)
    # mcp.run() owns the event loop; runs until the transport closes.
    mcp.run()


def run_sse() -> None:
    """Run the MCP server over SSE via Starlette/FastAPI.

    Intended to be mounted or run via uvicorn in production, behind an
    OAuth 2.1 reverse proxy (Kong).
    """
    client = _build_client()
    mcp = create_mcp_server(client)
    # FastMCP defaults to stdio if transport is unspecified; pin to sse here.
    mcp.run(transport="sse")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sse":
        run_sse()
    else:
        run_stdio()
