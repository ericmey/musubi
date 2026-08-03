"""Real-Qdrant contract for the one-shot non-embedding PATCH seam.

The API tests prove route semantics in local mode. These cases exercise the
server-side payload filters and exact-token ``delete_payload`` release whose
behavior must not be inferred from the in-memory client.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.immutable_vectors import (
    NonEmbeddingPatchConflict,
    patch_non_embedding_payload,
)
from musubi.store.names import collection_for_plane
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_COLLECTION = collection_for_plane("episodic")


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _seed(client: QdrantClient, *, importance: int = 5) -> tuple[str, str]:
    namespace = f"patch-{generate_ksuid()[:8].lower()}/dev/episodic"
    saved = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(
                namespace=namespace,
                content="one-shot real-qdrant contract",
                state="matured",
                importance=importance,
            )
        )
    )
    return namespace, saved.object_id


def _identity_filter(namespace: str, object_id: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
        ]
    )


def _payload(client: QdrantClient, namespace: str, object_id: str) -> dict[str, Any]:
    records, _ = client.scroll(
        collection_name=_COLLECTION,
        scroll_filter=_identity_filter(namespace, object_id),
        limit=2,
        with_payload=True,
        with_vectors=False,
    )
    assert len(records) == 1
    return dict(records[0].payload or {})


@pytest.mark.integration
def test_non_embedding_patch_stale_filtered_cas_loses_on_real_qdrant(
    real_qdrant: QdrantClient,
) -> None:
    namespace, object_id = _seed(real_qdrant)
    observed = _payload(real_qdrant, namespace, object_id)
    real_qdrant.set_payload(
        collection_name=_COLLECTION,
        payload={"importance": 7, "version": 2},
        points=_identity_filter(namespace, object_id),
        wait=True,
    )

    with pytest.raises(NonEmbeddingPatchConflict, match="lost its observed-version fence"):
        patch_non_embedding_payload(
            real_qdrant,
            _COLLECTION,
            namespace=namespace,
            object_id=object_id,
            observed_payload=observed,
            changes={"importance": 8},
            tag_mode="replace",
        )

    stored = _payload(real_qdrant, namespace, object_id)
    assert stored["importance"] == 7
    assert stored["version"] == 2
    assert "update_lease_token" not in stored


@pytest.mark.integration
def test_non_embedding_patch_release_removes_exact_token_on_real_qdrant(
    real_qdrant: QdrantClient,
) -> None:
    namespace, object_id = _seed(real_qdrant)
    observed = _payload(real_qdrant, namespace, object_id)

    published = patch_non_embedding_payload(
        real_qdrant,
        _COLLECTION,
        namespace=namespace,
        object_id=object_id,
        observed_payload=observed,
        changes={"tags": ["real-server"]},
        tag_mode="replace",
    )

    assert published["tags"] == ["real-server"]
    assert published["version"] == 2
    stored = _payload(real_qdrant, namespace, object_id)
    assert stored["tags"] == ["real-server"]
    assert stored["version"] == 2
    assert "update_lease_token" not in stored


@pytest.mark.integration
def test_non_embedding_patch_versionless_legacy_fence_lands_once_on_real_qdrant(
    real_qdrant: QdrantClient,
) -> None:
    namespace, object_id = _seed(real_qdrant)
    real_qdrant.delete_payload(
        collection_name=_COLLECTION,
        keys=["version"],
        points=_identity_filter(namespace, object_id),
        wait=True,
    )
    observed = _payload(real_qdrant, namespace, object_id)
    assert "version" not in observed

    published = patch_non_embedding_payload(
        real_qdrant,
        _COLLECTION,
        namespace=namespace,
        object_id=object_id,
        observed_payload=observed,
        changes={"importance": 9},
        tag_mode="replace",
    )

    assert published["importance"] == 9
    assert published["version"] == 1
    stored = _payload(real_qdrant, namespace, object_id)
    assert stored["importance"] == 9
    assert stored["version"] == 1
    assert "update_lease_token" not in stored
