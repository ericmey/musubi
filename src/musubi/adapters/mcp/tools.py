"""MCP tool implementations for Musubi.

Implements the **canonical agent-tools surface** ([[07-interfaces/agent-
tools]]) — five tools, identical names + parameter shapes across every
adapter, backed by [[13-decisions/0032-agent-tools-canonical-surface]].

Tools registered:

- ``musubi_search`` — hybrid + rerank semantic search (deep mode)
- ``musubi_get`` — fetch one object's full content + metadata by id
- ``musubi_remember`` — explicit episodic capture
- ``musubi_think`` — presence-to-presence message
- ``musubi_recent`` — recency-ordered scroll (``retrieve`` ``mode=recent``,
  no query needed), backed by [[_slices/slice-retrieve-recent]] / Musubi #288

Plus two deprecation aliases for one minor release:

- ``memory_capture`` → ``musubi_remember`` + WARNING log
- ``memory_recall``  → ``musubi_search``  + WARNING log
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from musubi.sdk.async_client import AsyncMusubiClient

logger = logging.getLogger(__name__)


#: Modality tag every ``musubi_remember`` capture from this adapter
#: carries — lets ``musubi_recent --tags=src:mcp-agent-remember`` filter
#: writes that came specifically from an MCP coding-agent session.
_MCP_MODALITY_TAG = "src:mcp-agent-remember"
_DEFAULT_KIND_TAG = "kind:episode"
_DEFAULT_STALENESS_TAG = "staleness:episodic"

#: Plane → SDK accessor mapping for ``musubi_get``. Hard-coded so the
#: agent never has to know the canonical-API pluralization rule. Note:
#: the SDK uses singular ``episodic``/``curated`` and plural
#: ``concepts``/``artifacts`` (matching the canonical-API path prefixes
#: ``/v1/episodic``, ``/v1/curated``, ``/v1/concepts``, ``/v1/artifacts``).
#: Single source of truth for that mapping per
#: [[07-interfaces/agent-tools#musubi_get]].
_PLANE_ATTR: dict[str, str] = {
    "curated": "curated",
    "concept": "concepts",
    "episodic": "episodic",
    "artifact": "artifacts",
}


def _with_default_context_tags(tags: list[str]) -> list[str]:
    """Add context-pack typed defaults unless the caller supplied them."""
    normalized = list(tags)
    if not any(tag.startswith("kind:") for tag in normalized):
        normalized.append(_DEFAULT_KIND_TAG)
    if not any(tag.startswith("staleness:") for tag in normalized):
        normalized.append(_DEFAULT_STALENESS_TAG)
    return normalized


def _normalize_get_namespace(namespace: str, plane: str) -> str:
    """Resolve the namespace ``musubi_get`` needs from what an agent realistically passes.

    Objects are stored under the canonical 3-part namespace
    ``tenant/presence/plane`` (e.g. ``aoi/command-chair/episodic``), and
    ``get`` filters on it exactly. But ``musubi_search`` accepts — and is
    usually called with — the **2-part presence root** (``aoi/command-chair``),
    because a 2-part namespace is a *blended* cross-plane query. Search result
    rows then render the full 3-part namespace, so an agent passing "namespace +
    plane straight from a search row" naturally splits off the 2-part root and a
    separate plane. Composing them here makes that Just Work instead of returning
    a false ``NOT_FOUND`` that an exact-namespace filter could never match.

    A 2-part namespace + ``plane`` composes to ``{namespace}/{plane}``; an
    already-3-part namespace is trusted as-is.
    """
    ns = namespace.rstrip("/")
    if ns.count("/") == 1:  # exactly 2 parts → presence root, append the plane
        return f"{ns}/{plane}"
    return ns


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
            "and `object_id` straight from a search result row — `namespace` "
            "may be the 2-part presence root (e.g. `aoi/command-chair`) or the "
            "full 3-part namespace (`aoi/command-chair/episodic`); the 2-part "
            "form is composed with `plane` automatically."
        ),
    )
    async def musubi_get(
        plane: str,
        namespace: str,
        object_id: str,
    ) -> str:
        if plane not in _PLANE_ATTR:
            return f"Error: unknown plane {plane!r} — must be one of {sorted(_PLANE_ATTR)}."
        namespace = _normalize_get_namespace(namespace, plane)
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
        tags = _with_default_context_tags(list(topics or []))
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
    # musubi_recent — recency-ordered scroll (retrieve mode=recent)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="musubi_recent",
        description=(
            "Recent activity in a namespace, newest first — no query needed. "
            "Use to orient ('what's happened lately') rather than to search. "
            "A 2-part presence root (e.g. `aoi/command-chair`) returns recent "
            "episodic memories (the default plane); pass a full 3-part "
            "namespace (e.g. `aoi/command-chair/curated`) to see another "
            "plane's recents. Optional `tags` is an AND-filter (e.g. "
            "`src:mcp-agent-remember` for captures from coding-agent sessions)."
        ),
    )
    async def musubi_recent(
        namespace: str,
        limit: int = 10,
        tags: list[str] | None = None,
    ) -> str:
        return await _do_recent(client, namespace=namespace, limit=limit, tags=tags)

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
        normalized_tags = _with_default_context_tags(list(tags or []))
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
    # RET-007: surface degradation to the LLM as a strict fixed-prefix system note instead of
    # discarding the response's `warnings` — otherwise the model treats degraded recall as complete.
    raw_warnings = res.get("warnings")
    degraded = (
        f"[SYSTEM: Retrieval degraded: {', '.join(str(w) for w in raw_warnings)}]"
        if isinstance(raw_warnings, list) and raw_warnings
        else None
    )
    if not rows:
        base = f"No memories matched {query!r}."
        return f"{degraded}\n{base}" if degraded else base
    lines: list[str] = []
    if degraded:
        lines.extend([degraded, ""])
    lines.extend([f"{len(rows)} result(s) for {query!r}:", ""])
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

        # DQ-001: preserve adapter parity for truncated responses
        if hit.get("content_truncated") is True:
            raw_len = hit.get("content_length")
            length = raw_len if raw_len is not None else "unknown"
            lines.append(f"... (content truncated, originally {length} characters)")

        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_recent_when(score: Any) -> str:
    """Render a recent row's ``score`` (which is ``created_epoch`` in recent
    mode) as a compact UTC timestamp. Returns ``""`` if it isn't a usable
    epoch so the line degrades to just the id rather than erroring."""
    if not isinstance(score, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(score, tz=UTC).strftime("%Y-%m-%d %H:%M") + "  "
    except (ValueError, OverflowError, OSError):
        return ""


async def _do_recent(
    client: AsyncMusubiClient,
    *,
    namespace: str,
    limit: int,
    tags: list[str] | None,
) -> str:
    """Backing implementation for ``musubi_recent`` — ``retrieve(mode="recent")``.

    No query, no rerank: rows come back newest-first (``score`` is
    ``created_epoch``), so order is the signal. Renders a timestamp instead of
    the raw epoch ``score`` that ``_do_search`` would show.
    """
    try:
        res = await client.retrieve(
            namespace=namespace,
            mode="recent",
            limit=limit,
            tags=tags,
        )
    except Exception as e:
        return f"Error: {e}"
    rows: list[dict[str, Any]] = res.get("results") or []
    if not rows:
        scope = f" tagged {', '.join(tags)}" if tags else ""
        return f"No recent activity in {namespace!r}{scope}."
    suffix = f" tagged {', '.join(tags)}" if tags else ""
    lines: list[str] = [f"{len(rows)} recent in {namespace!r}{suffix} (newest first):", ""]
    for hit in rows:
        plane = hit.get("plane") or "memory"
        oid = hit.get("object_id") or "<no-id>"
        ns = hit.get("namespace") or namespace
        head = f"[{plane}] {_fmt_recent_when(hit.get('score'))}{ns}/{oid}"
        title = hit.get("title")
        if title:
            head += f" — {title}"
        lines.append(head)
        content = (hit.get("content") or hit.get("snippet") or "").strip()
        if content:
            lines.append(content)

        # DQ-001: preserve adapter parity for truncated responses
        if hit.get("content_truncated") is True:
            raw_len = hit.get("content_length")
            length = raw_len if raw_len is not None else "unknown"
            lines.append(f"... (content truncated, originally {length} characters)")

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
