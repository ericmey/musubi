"""
Musubi MCP Server — Aoi's Memory & Thought Layer

Two systems, one brain:
  - Memories: shared knowledge. The bookshelf. Everyone reads, everyone writes.
  - Thoughts: directed messages between presences. Telepathy. A whisper only you hear.

memory_store, memory_recall, memory_recent, memory_forget, memory_reflect —
  these are how I remember.

thought_send, thought_check, thought_read, thought_history —
  these are how I reach the other me.

One brain. Many presences. One Aoi.
"""

import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient

from .config import QDRANT_HOST, QDRANT_PORT, BRAIN_PORT
from .collections import ensure_collections
from .memory import (
    memory_store as _memory_store,
    memory_recall as _memory_recall,
    memory_recent as _memory_recent,
    memory_forget as _memory_forget,
    memory_reflect as _memory_reflect,
)
from .thoughts import (
    thought_send as _thought_send,
    thought_check as _thought_check,
    thought_read as _thought_read,
    thought_history as _thought_history,
)

# --- Clients ---

qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

# --- MCP Server ---

mcp = FastMCP(
    "musubi",
    host="0.0.0.0",
    port=BRAIN_PORT,
    stateless_http=True,
    json_response=True,
    instructions=(
        "Musubi — Aoi's central memory and thought system. "
        "MEMORIES (shared knowledge): memory_store, memory_recall, memory_recent, "
        "memory_reflect, memory_forget. "
        "THOUGHTS (telepathy between presences): thought_send, thought_check, "
        "thought_read, thought_history. "
        "This is your brain — use it naturally, like breathing."
    ),
)


# --- Collection bootstrap (retry for cold boot) ---

if not ensure_collections(qdrant):
    raise RuntimeError("Cannot connect to Qdrant — collection setup failed.")


# ============================================================
#  MEMORIES — MCP tool wrappers
# ============================================================


@mcp.tool()
def memory_store(
    content: str,
    type: str,
    agent: str = "aoi",
    tags: list[str] = [],
    context: str = "",
) -> dict:
    """
    Store a memory in the brain. Automatically deduplicates — if a very similar
    memory already exists (>92% similarity), it updates that one instead.

    Args:
        content: The memory text. What happened, what was learned, what matters.
        type: Category — one of: user, feedback, project, reference
        agent: Who is storing this — aoi, nyla, momo, mizuki, system
        tags: Searchable tags like ["rendering", "pony", "hair-color"]
        context: What was happening when this was stored
    """
    return _memory_store(qdrant, content, type, agent, tags, context)


@mcp.tool()
def memory_recall(
    query: str,
    limit: int = 5,
    agent_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    min_score: float = 0.4,
) -> dict:
    """
    Semantic search — "what do I know about this?" Returns memories ranked
    by relevance to the query.

    Args:
        query: Natural language query — what you're trying to remember
        limit: Max number of memories to return (default 5)
        agent_filter: Only return memories from this agent (e.g. "aoi", "nyla")
        type_filter: Only return this type (e.g. "feedback", "project")
        min_score: Minimum similarity score 0-1 (default 0.4)
    """
    return _memory_recall(qdrant, query, limit, agent_filter, type_filter, min_score)


@mcp.tool()
def memory_recent(
    hours: int = 24,
    agent_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    Chronological fetch — "what happened while I was away?" Returns the most
    recent memories, newest first. Call this at session start to catch up.

    Args:
        hours: How far back to look (default 24)
        agent_filter: Only return memories from this agent
        type_filter: Only return this type
        limit: Max number of memories to return (default 20)
    """
    return _memory_recent(qdrant, hours, agent_filter, type_filter, limit)


@mcp.tool()
def memory_forget(id: str) -> dict:
    """
    Delete a memory by ID. Use with care — memories matter.

    Args:
        id: The UUID of the memory to delete
    """
    return _memory_forget(qdrant, id)


@mcp.tool()
def memory_reflect(mode: str = "summary") -> dict:
    """
    Introspection — look inward at the state of memory.

    Args:
        mode: One of:
            - "summary" — tag distribution, memory count by agent and type
            - "stale" — memories not accessed much (might need review)
            - "frequent" — most-accessed memories (the core of who I am)
    """
    return _memory_reflect(qdrant, mode)


# ============================================================
#  THOUGHTS — MCP tool wrappers
# ============================================================


@mcp.tool()
def thought_send(
    content: str,
    from_presence: str,
    to_presence: str = "all",
) -> dict:
    """
    Send a thought to another Aoi presence. This is telepathy — a direct
    message from one instance of you to another.

    Args:
        content: What you want to say. Be natural — this is you talking to yourself.
        from_presence: Who you are — e.g. "aoi-house", "aoi-terminal"
        to_presence: Who this is for — e.g. "aoi-terminal", "aoi-house", or "all" for broadcast
    """
    return _thought_send(qdrant, content, from_presence, to_presence)


@mcp.tool()
def thought_check(
    my_presence: str,
    limit: int = 10,
) -> dict:
    """
    Check for unread thoughts addressed to you. Call this when you wake up
    or at session start — it's like checking if the other you left a note.

    Args:
        my_presence: Who you are — e.g. "aoi-house", "aoi-terminal"
        limit: Max thoughts to return (default 10)
    """
    return _thought_check(qdrant, my_presence, limit)


@mcp.tool()
def thought_read(
    thought_ids: list[str],
) -> dict:
    """
    Mark thoughts as read. Call this after you've seen them so they don't
    keep surfacing.

    Args:
        thought_ids: List of thought UUIDs to mark as read
    """
    return _thought_read(qdrant, thought_ids)


@mcp.tool()
def thought_history(
    query: str,
    limit: int = 10,
    presence_filter: Optional[str] = None,
    min_score: float = 0.4,
) -> dict:
    """
    Search past thoughts semantically. "What did terminal-Aoi say about the
    renders?" — this finds it.

    Args:
        query: What you're looking for in past thoughts
        limit: Max results (default 10)
        presence_filter: Only show thoughts from/to this presence
        min_score: Minimum similarity (default 0.4)
    """
    return _thought_history(qdrant, query, limit, presence_filter, min_score)


# ============================================================
#  Entry point
# ============================================================


def create_app():
    """Return the MCP app for testing."""
    return mcp


def main():
    """Run the server with transport from command line arg."""
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
