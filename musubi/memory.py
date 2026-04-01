"""
Memory tools — shared knowledge, the bookshelf.

Pure business logic. No MCP/FastAPI dependencies.
Each function takes a qdrant client and uses embed_text for embeddings.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Condition,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
    models,
)

from .config import DUPLICATE_THRESHOLD, MEMORY_COLLECTION, THOUGHT_COLLECTION
from .embedding import embed_text

logger = logging.getLogger(__name__)

VALID_TYPES = ("user", "feedback", "project", "reference")


def _payload(point: Any) -> dict[str, Any]:
    """Extract payload from a Qdrant point, asserting it's not None."""
    payload: dict[str, Any] = point.payload
    assert payload is not None, f"Point {point.id} has no payload"
    return payload


def memory_store(
    qdrant: QdrantClient,
    content: str,
    type: str,
    agent: str = "aoi",
    tags: list[str] | None = None,
    context: str = "",
) -> dict:
    """
    Store a memory. Deduplicates automatically — if a very similar memory
    exists (>92% similarity), it updates that one instead.
    """
    if tags is None:
        tags = []

    if type not in VALID_TYPES:
        return {"error": f"type must be one of: {', '.join(VALID_TYPES)}"}

    try:
        vector = embed_text(content)
    except RuntimeError as e:
        return {"error": f"Embedding failed: {e}"}

    try:
        # Check for near-duplicates
        search_results = qdrant.query_points(
            collection_name=MEMORY_COLLECTION,
            query=vector,
            limit=1,
            score_threshold=DUPLICATE_THRESHOLD,
        )
    except Exception as e:
        return {"error": f"Qdrant search failed: {e}"}

    now = datetime.now(UTC).isoformat()
    now_epoch = datetime.now(UTC).timestamp()

    try:
        if search_results.points:
            existing = search_results.points[0]
            existing_payload = _payload(existing)
            existing_payload["content"] = content
            existing_payload["updated_at"] = now
            existing_payload["tags"] = list(set(existing_payload.get("tags", []) + tags))
            if context:
                existing_payload["context"] = context

            qdrant.upsert(
                collection_name=MEMORY_COLLECTION,
                points=[
                    PointStruct(
                        id=str(existing.id),
                        vector=vector,
                        payload=existing_payload,
                    )
                ],
            )
            return {
                "status": "updated",
                "id": str(existing.id),
                "similarity": existing.score,
            }

        # New memory
        memory_id = str(uuid.uuid4())
        payload = {
            "content": content,
            "type": type,
            "agent": agent,
            "tags": tags,
            "context": context,
            "created_at": now,
            "created_epoch": now_epoch,
            "updated_at": now,
            "access_count": 0,
            "last_accessed": None,
        }

        qdrant.upsert(
            collection_name=MEMORY_COLLECTION,
            points=[
                PointStruct(
                    id=memory_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
        return {"status": "stored", "id": memory_id}

    except Exception as e:
        return {"error": f"Qdrant upsert failed: {e}"}


def memory_recall(
    qdrant: QdrantClient,
    query: str,
    limit: int = 5,
    agent_filter: str | None = None,
    type_filter: str | None = None,
    min_score: float = 0.4,
) -> dict:
    """
    Semantic search — returns memories ranked by relevance to the query.
    """
    try:
        vector = embed_text(query)
    except RuntimeError as e:
        return {"error": f"Embedding failed: {e}"}

    conditions: list[Condition] = []
    if agent_filter:
        conditions.append(FieldCondition(key="agent", match=MatchValue(value=agent_filter)))
    if type_filter:
        conditions.append(FieldCondition(key="type", match=MatchValue(value=type_filter)))

    query_filter = Filter(must=conditions) if conditions else None

    try:
        results = qdrant.query_points(
            collection_name=MEMORY_COLLECTION,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
        )
    except Exception as e:
        return {"error": f"Qdrant query failed: {e}"}

    # Update access counts
    for point in results.points:
        try:
            now = datetime.now(UTC).isoformat()
            qdrant.set_payload(
                collection_name=MEMORY_COLLECTION,
                payload={
                    "access_count": (_payload(point).get("access_count", 0) + 1),
                    "last_accessed": now,
                },
                points=[str(point.id)],
            )
        except Exception as e:
            logger.warning("Failed to update access count for %s: %s", point.id, e)

    return {
        "memories": [
            {
                "id": str(p.id),
                "content": _payload(p).get("content", ""),
                "type": _payload(p).get("type", ""),
                "agent": _payload(p).get("agent", ""),
                "tags": _payload(p).get("tags", []),
                "context": _payload(p).get("context", ""),
                "score": round(p.score, 4),
                "created_at": _payload(p).get("created_at", ""),
            }
            for p in results.points
        ]
    }


def memory_recent(
    qdrant: QdrantClient,
    hours: int = 24,
    agent_filter: str | None = None,
    type_filter: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Chronological fetch — most recent memories, newest first.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()

    conditions: list[Condition] = [FieldCondition(key="created_epoch", range=Range(gte=cutoff))]
    if agent_filter:
        conditions.append(FieldCondition(key="agent", match=MatchValue(value=agent_filter)))
    if type_filter:
        conditions.append(FieldCondition(key="type", match=MatchValue(value=type_filter)))

    try:
        results = qdrant.scroll(
            collection_name=MEMORY_COLLECTION,
            scroll_filter=Filter(must=conditions),
            limit=limit,
            with_payload=True,
            with_vectors=False,
            order_by=models.OrderBy(key="created_epoch", direction=models.Direction.DESC),
        )
    except Exception as e:
        return {"error": f"Qdrant scroll failed: {e}"}

    points = results[0] if results else []

    return {
        "memories": [
            {
                "id": str(p.id),
                "content": _payload(p).get("content", ""),
                "type": _payload(p).get("type", ""),
                "agent": _payload(p).get("agent", ""),
                "tags": _payload(p).get("tags", []),
                "context": _payload(p).get("context", ""),
                "created_at": _payload(p).get("created_at", ""),
            }
            for p in points
        ]
    }


def memory_forget(qdrant: QdrantClient, id: str) -> dict:
    """Delete a memory by ID."""
    try:
        qdrant.delete(
            collection_name=MEMORY_COLLECTION,
            points_selector=models.PointIdsList(points=[id]),
        )
        return {"status": "forgotten", "id": id}
    except Exception as e:
        return {"error": f"Could not forget: {e}"}


def memory_reflect(qdrant: QdrantClient, mode: str = "summary") -> dict:
    """
    Introspection — look inward at the state of memory.
    Modes: summary, stale, frequent.
    """
    if mode == "summary":
        return _reflect_summary(qdrant)
    elif mode == "stale":
        return _reflect_stale(qdrant)
    elif mode == "frequent":
        return _reflect_frequent(qdrant)
    return {"error": "mode must be one of: summary, stale, frequent"}


def _reflect_summary(qdrant: QdrantClient) -> dict:
    try:
        mem_info = qdrant.get_collection(MEMORY_COLLECTION)
        thought_info = qdrant.get_collection(THOUGHT_COLLECTION)

        all_points, _ = qdrant.scroll(
            collection_name=MEMORY_COLLECTION,
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )

        agents: dict[str, int] = {}
        types: dict[str, int] = {}
        tags: dict[str, int] = {}
        for p in all_points:
            agent = _payload(p).get("agent", "unknown")
            agents[agent] = agents.get(agent, 0) + 1
            mtype = _payload(p).get("type", "unknown")
            types[mtype] = types.get(mtype, 0) + 1
            for tag in _payload(p).get("tags", []):
                tags[tag] = tags.get(tag, 0) + 1

        return {
            "total_memories": mem_info.points_count,
            "total_thoughts": thought_info.points_count,
            "by_agent": agents,
            "by_type": types,
            "top_tags": dict(sorted(tags.items(), key=lambda x: x[1], reverse=True)[:20]),
        }
    except Exception as e:
        return {"error": f"Reflect summary failed: {e}"}


def _reflect_stale(qdrant: QdrantClient) -> dict:
    try:
        stale, _ = qdrant.scroll(
            collection_name=MEMORY_COLLECTION,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="access_count", range=Range(lte=1)),
                ]
            ),
            limit=20,
            with_payload=True,
            with_vectors=False,
        )

        return {
            "stale_memories": [
                {
                    "id": str(p.id),
                    "content": _payload(p).get("content", "")[:100],
                    "agent": _payload(p).get("agent", ""),
                    "created_at": _payload(p).get("created_at", ""),
                    "access_count": _payload(p).get("access_count", 0),
                }
                for p in stale
            ]
        }
    except Exception as e:
        return {"error": f"Reflect stale failed: {e}"}


def _reflect_frequent(qdrant: QdrantClient) -> dict:
    try:
        frequent, _ = qdrant.scroll(
            collection_name=MEMORY_COLLECTION,
            limit=20,
            with_payload=True,
            with_vectors=False,
            order_by=models.OrderBy(key="access_count", direction=models.Direction.DESC),
        )

        return {
            "core_memories": [
                {
                    "id": str(p.id),
                    "content": _payload(p).get("content", "")[:100],
                    "agent": _payload(p).get("agent", ""),
                    "access_count": _payload(p).get("access_count", 0),
                    "type": _payload(p).get("type", ""),
                }
                for p in frequent
            ]
        }
    except Exception as e:
        return {"error": f"Reflect frequent failed: {e}"}
