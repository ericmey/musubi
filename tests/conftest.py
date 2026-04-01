"""
Shared test fixtures — mock Qdrant client and mock embeddings.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

VECTOR_SIZE = 3072


def make_fake_vector():
    """Return a deterministic fake 3072-dim vector."""
    return [0.01] * VECTOR_SIZE


class FakePoint:
    """Mimics a Qdrant ScoredPoint / Record."""

    def __init__(self, id=None, payload=None, score=0.95):
        self.id = id or str(uuid.uuid4())
        self.payload = payload or {}
        self.score = score


class FakeQueryResult:
    """Mimics qdrant query_points result."""

    def __init__(self, points=None):
        self.points = points or []


class FakeCollectionInfo:
    """Mimics qdrant get_collection result."""

    def __init__(self, points_count=0):
        self.points_count = points_count


class FakeCollectionList:
    """Mimics qdrant get_collections result."""

    def __init__(self, names=None):
        self.collections = [
            type("Col", (), {"name": n})() for n in (names or [])
        ]


@pytest.fixture
def mock_qdrant():
    """A MagicMock QdrantClient with sensible defaults."""
    client = MagicMock()

    # Default: query_points returns empty (no duplicates)
    client.query_points.return_value = FakeQueryResult(points=[])

    # Default: scroll returns empty list
    client.scroll.return_value = ([], None)

    # Default: get_collections returns both collections exist
    client.get_collections.return_value = FakeCollectionList(
        names=["musubi_memories", "musubi_thoughts"]
    )

    # Default: get_collection returns info
    client.get_collection.return_value = FakeCollectionInfo(points_count=42)

    return client


@pytest.fixture
def mock_embed():
    """Patch embed_text to return fake vectors without calling Gemini."""
    with patch("musubi.memory.embed_text", side_effect=lambda t: make_fake_vector()):
        with patch("musubi.thoughts.embed_text", side_effect=lambda t: make_fake_vector()):
            yield make_fake_vector
