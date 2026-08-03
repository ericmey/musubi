"""IDEM-007 evidence-gated sibling of the ordinary non-embedding PATCH seam."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.store import bootstrap
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory, RetractionEvidence

_COLLECTION = "musubi_episodic"
_NS = "eric/claude-code/episodic"
_ORIGINAL = "the exact false claim retained for retrieval"


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _vectors(content: str) -> dict[str, Any]:
    embedder = FakeEmbedder()
    dense = asyncio.run(embedder.embed_dense([content]))[0]
    sparse = asyncio.run(embedder.embed_sparse([content]))[0]
    return {
        DENSE_VECTOR_NAME: dense,
        SPARSE_VECTOR_NAME: models.SparseVector(indices=list(sparse), values=list(sparse.values())),
    }


def _seed_v2(client: QdrantClient) -> tuple[str, dict[str, Any], dict[str, Any]]:
    object_id = generate_ksuid()
    anchor_point = str(uuid.uuid4())
    content_point = str(uuid.uuid4())
    generation = "generation-1"
    base = EpisodicMemory(
        namespace=_NS,
        object_id=object_id,
        content=_ORIGINAL,
        summary="false summary",
        state="matured",
    ).model_dump(mode="json")
    anchor = {
        **base,
        "point_kind": "anchor",
        "live_point": content_point,
        "pointer_version": 1,
        "committed_operation_id": generation,
        "vector_layout_version": 2,
    }
    content = {
        "object_id": object_id,
        "namespace": _NS,
        "point_kind": "content",
        "generation": generation,
        "owner_token": "owner-1",
        "content": _ORIGINAL,
        "summary": "false summary",
    }
    client.upsert(
        collection_name=_COLLECTION,
        points=[
            models.PointStruct(id=anchor_point, payload=anchor, vector=_vectors(_ORIGINAL)),
            models.PointStruct(id=content_point, payload=content, vector=_vectors(_ORIGINAL)),
        ],
        wait=True,
    )
    return object_id, anchor, content


def _rows(client: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    rows, _ = client.scroll(
        collection_name=_COLLECTION,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=4,
        with_payload=True,
        with_vectors=True,
    )
    return sorted(
        [{"payload": dict(row.payload or {}), "vector": row.vector} for row in rows],
        key=lambda row: row["payload"]["point_kind"],
    )


def _evidence(anchor: dict[str, Any]) -> RetractionEvidence:
    import hashlib

    original = _ORIGINAL.encode()
    return RetractionEvidence.model_validate(
        {
            "kind": "artifact_escrow_v1",
            "artifact_namespace": "eric/claude-code/artifact",
            "artifact_ref": {"artifact_id": generate_ksuid()},
            "original_sha256": hashlib.sha256(original).hexdigest(),
            "original_utf8_bytes": len(original),
            "quoted_prefix_utf8_bytes": len(original),
            "omitted_bytes": 0,
            "vector_basis": "original",
            "preserved_pointer": {"kind": "v2", "live_point": anchor["live_point"]},
            "operation_identity_hash": "1" * 64,
            "request_digest": "2" * 64,
        }
    )


def _retract(
    client: QdrantClient,
    *,
    object_id: str,
    anchor: dict[str, Any],
    content: dict[str, Any],
    evidence: RetractionEvidence,
) -> dict[str, Any]:
    module = importlib.import_module("musubi.store.immutable_vectors")
    retract = cast(
        Callable[..., dict[str, Any]],
        getattr(module, "retract_non_embedding_payload"),
    )
    return retract(
        client,
        _COLLECTION,
        namespace=_NS,
        object_id=object_id,
        observed_payload=anchor,
        target_payload=content,
        changes={
            "content": f"RETRACTED\nOriginal excerpt:\n{_ORIGINAL}\nCorrected truth: truth",
            "summary": "retracted false claim",
            "importance": 1,
        },
        evidence=evidence,
    )


def test_retraction_entrypoint_writes_anchor_only_and_preserves_generation_and_vectors(
    qdrant: QdrantClient,
) -> None:
    object_id, anchor, content = _seed_v2(qdrant)
    before = _rows(qdrant, object_id)
    published = _retract(
        qdrant,
        object_id=object_id,
        anchor=anchor,
        content=content,
        evidence=_evidence(anchor),
    )
    after = _rows(qdrant, object_id)
    assert published["content"].startswith("RETRACTED")
    assert published["committed_operation_id"] == anchor["committed_operation_id"]
    assert after[1] == before[1], "immutable content payload and vectors are whole-row invariant"
    assert after[0]["vector"] == before[0]["vector"]
    assert "update_lease_token" not in after[0]["payload"]


def test_retraction_entrypoint_refuses_evidence_that_shared_keyhole_would_reject(
    qdrant: QdrantClient,
) -> None:
    object_id, anchor, content = _seed_v2(qdrant)
    before = _rows(qdrant, object_id)
    invalid = _evidence(anchor).model_copy(update={"original_sha256": "f" * 64})
    with pytest.raises(ValueError, match="original_sha256"):
        _retract(
            qdrant,
            object_id=object_id,
            anchor=anchor,
            content=content,
            evidence=invalid,
        )
    assert _rows(qdrant, object_id) == before


def test_ordinary_patch_still_cannot_enable_v2_projection_by_flag(qdrant: QdrantClient) -> None:
    from musubi.store.immutable_vectors import patch_non_embedding_payload

    object_id, anchor, _content = _seed_v2(qdrant)
    with pytest.raises(TypeError):
        cast(Callable[..., Any], patch_non_embedding_payload)(
            qdrant,
            _COLLECTION,
            namespace=_NS,
            object_id=object_id,
            observed_payload=anchor,
            changes={"content": "attempted bypass"},
            tag_mode="replace",
            allow_projection=True,
        )
