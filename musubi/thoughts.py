"""
Thought tools — telepathy between presences.

Pure business logic. No MCP/FastAPI dependencies.
Each function takes a qdrant client and uses embed_text for embeddings.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    models,
)

from .config import THOUGHT_COLLECTION
from .embedding import embed_text

logger = logging.getLogger(__name__)


def thought_send(
    qdrant: QdrantClient,
    content: str,
    from_presence: str,
    to_presence: str = "all",
) -> dict:
    """
    Send a thought to another Aoi presence.
    """
    try:
        vector = embed_text(content)
    except RuntimeError as e:
        return {"error": f"Embedding failed: {e}"}

    now = datetime.now(timezone.utc).isoformat()
    now_epoch = datetime.now(timezone.utc).timestamp()

    thought_id = str(uuid.uuid4())
    payload = {
        "content": content,
        "from_presence": from_presence,
        "to_presence": to_presence,
        "read": False,
        "created_at": now,
        "created_epoch": now_epoch,
    }

    try:
        qdrant.upsert(
            collection_name=THOUGHT_COLLECTION,
            points=[
                PointStruct(
                    id=thought_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
    except Exception as e:
        return {"error": f"Qdrant upsert failed: {e}"}

    return {
        "status": "sent",
        "id": thought_id,
        "from": from_presence,
        "to": to_presence,
    }


def thought_check(
    qdrant: QdrantClient,
    my_presence: str,
    limit: int = 10,
) -> dict:
    """
    Check for unread thoughts addressed to you.
    """
    try:
        results = qdrant.scroll(
            collection_name=THOUGHT_COLLECTION,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="read", match=MatchValue(value=False)),
                ],
                should=[
                    FieldCondition(
                        key="to_presence", match=MatchValue(value=my_presence)
                    ),
                    FieldCondition(
                        key="to_presence", match=MatchValue(value="all")
                    ),
                ],
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
            order_by=models.OrderBy(
                key="created_epoch", direction=models.Direction.DESC
            ),
        )
    except Exception as e:
        return {"error": f"Qdrant scroll failed: {e}"}

    points = results[0] if results else []

    # Don't show thoughts I sent to myself as unread
    thoughts = [
        p for p in points if p.payload.get("from_presence") != my_presence
    ]

    return {
        "unread_count": len(thoughts),
        "thoughts": [
            {
                "id": str(p.id),
                "content": p.payload.get("content", ""),
                "from": p.payload.get("from_presence", ""),
                "to": p.payload.get("to_presence", ""),
                "created_at": p.payload.get("created_at", ""),
            }
            for p in thoughts
        ],
    }


def thought_read(
    qdrant: QdrantClient,
    thought_ids: list[str],
) -> dict:
    """
    Mark thoughts as read.
    """
    marked = 0
    for tid in thought_ids:
        try:
            qdrant.set_payload(
                collection_name=THOUGHT_COLLECTION,
                payload={"read": True},
                points=[tid],
            )
            marked += 1
        except Exception:
            pass

    return {"status": "read", "marked": marked, "total": len(thought_ids)}


def thought_history(
    qdrant: QdrantClient,
    query: str,
    limit: int = 10,
    presence_filter: Optional[str] = None,
    min_score: float = 0.4,
) -> dict:
    """
    Search past thoughts semantically.
    """
    try:
        vector = embed_text(query)
    except RuntimeError as e:
        return {"error": f"Embedding failed: {e}"}

    conditions = []
    if presence_filter:
        conditions.append(
            Filter(
                should=[
                    FieldCondition(
                        key="from_presence",
                        match=MatchValue(value=presence_filter),
                    ),
                    FieldCondition(
                        key="to_presence",
                        match=MatchValue(value=presence_filter),
                    ),
                ]
            )
        )

    query_filter = Filter(must=conditions) if conditions else None

    try:
        results = qdrant.query_points(
            collection_name=THOUGHT_COLLECTION,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
        )
    except Exception as e:
        return {"error": f"Qdrant query failed: {e}"}

    return {
        "thoughts": [
            {
                "id": str(p.id),
                "content": p.payload.get("content", ""),
                "from": p.payload.get("from_presence", ""),
                "to": p.payload.get("to_presence", ""),
                "score": round(p.score, 4),
                "created_at": p.payload.get("created_at", ""),
                "read": p.payload.get("read", False),
            }
            for p in results.points
        ]
    }
