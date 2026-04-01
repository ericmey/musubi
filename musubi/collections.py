"""
Qdrant collection setup and management.
"""

import logging
import time

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    models,
)

from .config import MEMORY_COLLECTION, THOUGHT_COLLECTION, VECTOR_SIZE

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_DELAY = 2  # seconds


def ensure_collections(qdrant: QdrantClient) -> bool:
    """
    Create both collections if they don't exist.

    Retries up to 5 times for cold-boot scenarios where Qdrant is still starting.
    Returns True on success, False on failure.
    """
    for attempt in range(MAX_RETRIES):
        try:
            _create_if_missing(qdrant)
            return True
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    "Qdrant connection attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt + 1, MAX_RETRIES, e, RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                logger.error(
                    "Cannot connect to Qdrant after %d attempts: %s",
                    MAX_RETRIES, e,
                )
                return False


def _create_if_missing(qdrant: QdrantClient) -> None:
    """Create collections and payload indexes if they don't exist."""
    collections = [c.name for c in qdrant.get_collections().collections]

    # Memory collection
    if MEMORY_COLLECTION not in collections:
        qdrant.create_collection(
            collection_name=MEMORY_COLLECTION,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        qdrant.create_payload_index(
            MEMORY_COLLECTION, "agent", models.PayloadSchemaType.KEYWORD
        )
        qdrant.create_payload_index(
            MEMORY_COLLECTION, "type", models.PayloadSchemaType.KEYWORD
        )
        qdrant.create_payload_index(
            MEMORY_COLLECTION, "created_at", models.PayloadSchemaType.KEYWORD
        )
        qdrant.create_payload_index(
            MEMORY_COLLECTION, "created_epoch", models.PayloadSchemaType.FLOAT
        )
        qdrant.create_payload_index(
            MEMORY_COLLECTION, "access_count", models.PayloadSchemaType.INTEGER
        )

    # Thought collection
    if THOUGHT_COLLECTION not in collections:
        qdrant.create_collection(
            collection_name=THOUGHT_COLLECTION,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        qdrant.create_payload_index(
            THOUGHT_COLLECTION, "from_presence", models.PayloadSchemaType.KEYWORD
        )
        qdrant.create_payload_index(
            THOUGHT_COLLECTION, "to_presence", models.PayloadSchemaType.KEYWORD
        )
        qdrant.create_payload_index(
            THOUGHT_COLLECTION, "read", models.PayloadSchemaType.BOOL
        )
        qdrant.create_payload_index(
            THOUGHT_COLLECTION, "created_epoch", models.PayloadSchemaType.FLOAT
        )
