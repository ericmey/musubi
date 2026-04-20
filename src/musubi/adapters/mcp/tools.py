"""MCP tool implementations for Musubi.

These maps a flat tool signature to the correct Musubi SDK call.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from musubi.sdk.async_client import AsyncMusubiClient

logger = logging.getLogger(__name__)

# To use these cleanly without passing `client` into every tool manually
# (which MCP wouldn't know how to inject at runtime automatically),
# the MCP setup needs to bind them or use a closure.
# For simplicity with FastMCP, we'll construct the FastMCP instance in a
# factory function that has the client in scope.


def attach_tools(mcp: FastMCP, client: AsyncMusubiClient) -> None:
    """Register all Musubi tools on the given FastMCP server."""

    @mcp.tool(name="memory_capture", description="Capture a new episodic observation.")
    async def memory_capture(
        namespace: str, content: str, importance: int = 5, tags: list[str] | None = None
    ) -> str:
        try:
            res = await client.memories.capture(
                namespace=namespace,
                content=content,
                importance=importance,
                tags=tags or [],
            )
            return f"Captured successfully with id {res['object_id']}"
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool(name="memory_recall", description="Retrieve recent memories for context.")
    async def memory_recall(namespace: str, query: str, limit: int = 10) -> str:
        try:
            res = await client.retrieve(
                namespace=namespace,
                query_text=query,
                mode="fast",
                limit=limit,
            )
            lines = []
            for hit in res["results"]:
                lines.append(f"[{hit['plane']}] {hit['object_id']}: {hit['snippet']}")
            return "\n".join(lines) if lines else "No relevant memories found."
        except Exception as e:
            return f"Error: {e}"
