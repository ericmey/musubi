"""MCP tool implementations for Musubi.

Implements the **canonical agent-tools surface** ([[07-interfaces/agent-
tools]]) — five tools, identical names + parameter shapes across every
adapter, backed by [[13-decisions/0032-agent-tools-canonical-surface]].

Tools registered:

- ``musubi_search`` — hybrid + rerank semantic search (deep mode)
- ``musubi_get`` — fetch one object's full content + metadata by id
- ``musubi_remember`` — explicit episodic capture
- ``musubi_think`` — presence-to-presence message
- ``musubi_recent`` — recency-ordered scroll
  (currently a deferred stub — depends on [[_slices/slice-retrieve-
  recent]] / Musubi #288 for ``mode=recent``)

Plus two deprecation aliases for one minor release:

- ``memory_capture`` → ``musubi_remember`` + WARNING log
- ``memory_recall``  → ``musubi_search``  + WARNING log
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from musubi.sdk.async_client import AsyncMusubiClient

logger = logging.getLogger(__name__)


#: Modality tag every ``musubi_remember`` capture from this adapter
#: carries — lets ``musubi_recent --tags=src:mcp-agent-remember`` filter
#: writes that came specifically from an MCP coding-agent session.
_MCP_MODALITY_TAG = "src:mcp-agent-remember"

#: Plane → SDK accessor mapping for ``musubi_get``. Hard-coded so the
#: agent never has to know the canonical-API pluralization rule.
_PLANE_ATTR: dict[str, str] = {
    "curated": "curated",
    "concept": "concept",
    "episodic": "episodic",
    "artifact": "artifact",
}


def attach_tools(mcp: FastMCP, client: AsyncMusubiClient) -> None:
    """Register every canonical agent tool on the given FastMCP server.

    Tool implementations are bound via closure over ``client`` so each
    invocation talks to the same underlying SDK instance — matching the
    pattern in :func:`musubi.adapters.mcp.server.create_mcp_server`.
    """

    # ------------------------------------------------------------------
    # musubi_search — hybrid + rerank semantic search
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_search",
        description=(
            "Hybrid + rerank semantic search across Musubi. Use when the "
            "passive memory supplement didn't surface what you need. "
            "Returns ranked results with [plane], score, and content snippet."
        ),
    )
    async def musubi_search(
        namespace: str,
        query: str,
        limit: int = 5,
        planes: list[str] | None = None,
    ) -> str:
        return await _do_search(
            client, namespace=namespace, query=query, limit=limit, planes=planes
        )

    # ------------------------------------------------------------------
    # musubi_get — fetch one object's full content by id
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_get",
        description=(
            "Fetch one Musubi object's full content + metadata by id. "
            "Use after `musubi_search` when a snippet looks load-bearing "
            "and you need the underlying source. Pass `plane`, `namespace`, "
            "and `object_id` straight from a search result row."
        ),
    )
    async def musubi_get(
        plane: str,
        namespace: str,
        object_id: str,
    ) -> str:
        if plane not in _PLANE_ATTR:
            return f"Error: unknown plane {plane!r} — must be one of {sorted(_PLANE_ATTR)}."
        plane_client = getattr(client, _PLANE_ATTR[plane])
        try:
            row = await plane_client.get(namespace=namespace, object_id=object_id)
        except Exception as e:
            return f"Error: {e}"
        return _format_object(plane=plane, namespace=namespace, object_id=object_id, row=row)

    # ------------------------------------------------------------------
    # musubi_remember — explicit episodic capture
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_remember",
        description=(
            "Capture something into Musubi episodic memory. Use for things "
            "you judge load-bearing — decisions, facts, commitments. "
            "Default importance is 7 (above the passive-capture baseline of 5)."
        ),
    )
    async def musubi_remember(
        namespace: str,
        content: str,
        importance: int = 7,
        topics: list[str] | None = None,
    ) -> str:
        # Adapter modality tag is auto-added so cross-modality recent / search
        # can filter by source per [[07-interfaces/agent-tools#modality-tagging]].
        tags = list(topics or [])
        if _MCP_MODALITY_TAG not in tags:
            tags.append(_MCP_MODALITY_TAG)
        try:
            res = await client.episodic.capture(
                namespace=namespace,
                content=content,
                importance=importance,
                tags=tags,
            )
        except Exception as e:
            return f"Error: {e}"
        return f"Remembered in Musubi episodic ({namespace}) — id {res['object_id']}."

    # ------------------------------------------------------------------
    # musubi_think — presence-to-presence message
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_think",
        description=(
            "Send a presence-to-presence thought. The recipient's next turn "
            "sees the message as inbound context. Use when the user wants you "
            "to tell another agent (Aoi, Nyla, voice, …) something."
        ),
    )
    async def musubi_think(
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> str:
        try:
            res = await client.thoughts.send(
                namespace=namespace,
                from_presence=from_presence,
                to_presence=to_presence,
                content=content,
                channel=channel,
                importance=importance,
            )
        except Exception as e:
            return f"Error: {e}"
        return f"Sent to {to_presence}. (id={res['object_id']})"

    # ------------------------------------------------------------------
    # musubi_recent — deferred stub (#288 / slice-retrieve-recent)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_recent",
        description=(
            "Recent activity, recency-ordered, no query needed. NOT YET "
            "WIRED — depends on slice-retrieve-recent (Musubi #288). The "
            "tool registers so its name is reserved at the canonical "
            "surface; calls return a clear deferred message until the "
            "backend ships."
        ),
    )
    async def musubi_recent(
        namespace: str,
        limit: int = 10,
    ) -> str:
        # Per [[CLAUDE#prohibited-patterns]]: ADR-punted dependencies fail
        # loud. This tool's contract is canonical, but the backend it needs
        # (`mode=recent`) is on the way. We return a clearly user-readable
        # message rather than silently no-op, and we log at WARNING so the
        # operator notices repeated calls in degraded mode.
        logger.warning(
            "musubi_recent invoked but is not yet available "
            "(deferred to slice-retrieve-recent / Musubi #288)"
        )
        return (
            "musubi_recent is not yet available — its backend dependency "
            "(slice-retrieve-recent, Musubi #288) hasn't shipped. Until it does, "
            "use `musubi_search` with a date- or recency-flavored query as a "
            "workaround. Tracking issue: "
            "https://github.com/ericmey/musubi/issues/288"
        )

    # ------------------------------------------------------------------
    # Deprecation aliases — one minor release, then drop
    # ------------------------------------------------------------------

    @mcp.tool(
        name="memory_capture",
        description=(
            "[DEPRECATED] Use `musubi_remember`. "
            "Capture a new episodic memory observation in Musubi."
        ),
    )
    async def memory_capture(
        namespace: str,
        content: str,
        importance: int = 5,
        tags: list[str] | None = None,
    ) -> str:
        logger.warning(
            "memory_capture is deprecated; use musubi_remember (canonical name "
            "per ADR 0032 / agent-tools spec). The alias drops in the next minor release."
        )
        # Forward through the canonical body. Existing memory_capture
        # default importance was 5; preserve it on the alias path so
        # behavior doesn't change for legacy callers.
        normalized_tags = list(tags or [])
        if _MCP_MODALITY_TAG not in normalized_tags:
            normalized_tags.append(_MCP_MODALITY_TAG)
        try:
            res = await client.episodic.capture(
                namespace=namespace,
                content=content,
                importance=importance,
                tags=normalized_tags,
            )
        except Exception as e:
            return f"Error: {e}"
        return f"Captured successfully with id {res['object_id']}"

    @mcp.tool(
        name="memory_recall",
        description=("[DEPRECATED] Use `musubi_search`. Retrieve memories matching a query."),
    )
    async def memory_recall(
        namespace: str,
        query: str,
        limit: int = 10,
    ) -> str:
        logger.warning(
            "memory_recall is deprecated; use musubi_search (canonical name "
            "per ADR 0032 / agent-tools spec). The alias drops in the next minor release."
        )
        return await _do_search(client, namespace=namespace, query=query, limit=limit, planes=None)


# ----------------------------------------------------------------------
# Shared bodies — invoked from both canonical tools and aliases
# ----------------------------------------------------------------------


async def _do_search(
    client: AsyncMusubiClient,
    *,
    namespace: str,
    query: str,
    limit: int,
    planes: list[str] | None,
) -> str:
    """Backing implementation for ``musubi_search`` and ``memory_recall``."""
    try:
        res = await client.retrieve(
            namespace=namespace,
            query_text=query,
            mode="deep",
            limit=limit,
            planes=planes,
        )
    except Exception as e:
        return f"Error: {e}"
    rows: list[dict[str, Any]] = res.get("results") or []
    if not rows:
        return f"No memories matched {query!r}."
    lines: list[str] = [f"{len(rows)} result(s) for {query!r}:", ""]
    for hit in rows:
        plane = hit.get("plane") or "memory"
        score = hit.get("score")
        score_str = f" (score {score:.2f})" if isinstance(score, (int, float)) else ""
        oid = hit.get("object_id") or "<no-id>"
        ns = hit.get("namespace") or namespace
        title_or_oid = hit.get("title") or f"{ns}/{oid}"
        lines.append(f"[{plane}]{score_str} {title_or_oid}")
        content = (hit.get("content") or hit.get("snippet") or "").strip()
        if content:
            lines.append(content)
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_object(
    *,
    plane: str,
    namespace: str,
    object_id: str,
    row: dict[str, Any],
) -> str:
    """Render a fetched object for the agent — stable header + canonical
    metadata + body. Same shape as the openclaw-musubi `musubi_get` so
    the rendering is identical across adapters."""
    lines: list[str] = [f"[{plane}] {namespace}/{object_id}", ""]
    header_keys = (
        "title",
        "state",
        "importance",
        "event_at",
        "ingested_at",
        "created_at",
        "updated_at",
        "modality",
        "source_context",
        "vault_path",
        "topics",
        "tags",
        "participants",
    )
    seen: set[str] = {"namespace", "object_id", "content", "summary", "body"}
    for key in header_keys:
        if key not in row:
            continue
        seen.add(key)
        value = row[key]
        if value in (None, []):
            continue
        lines.append(f"{key}: {_render_value(value)}")
    for key in sorted(set(row.keys()) - seen):
        value = row[key]
        if value in (None, []):
            continue
        lines.append(f"{key}: {_render_value(value)}")
    body = _pick_content(row)
    if body is not None:
        lines.append("")
        lines.append(body)
    return "\n".join(lines).rstrip()


def _pick_content(row: dict[str, Any]) -> str | None:
    for key in ("content", "body", "summary"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return None


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_render_value(v) for v in value)
    return str(value)
