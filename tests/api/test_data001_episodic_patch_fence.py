"""DATA-001 #634: fenced, layout-aware, non-reembedding episodic PATCH."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.planes.episodic import EpisodicPlane
from musubi.store.immutable_vectors import ImmutableVectorPublisher
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_NS = "eric/claude-code/episodic"
_OTHER_NS = "other-agent/other-presence/episodic"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(
    *, namespace: str, object_id: str, content: str, importance: int = 3
) -> dict[str, Any]:
    return EpisodicMemory(
        namespace=namespace,
        object_id=object_id,
        content=content,
        importance=importance,
        tags=["existing", "remove-me"],
    ).model_dump(mode="json")


def _vectors(content: str) -> dict[str, Any]:
    embedder = FakeEmbedder()
    dense = asyncio.run(embedder.embed_dense([content]))[0]
    sparse = asyncio.run(embedder.embed_sparse([content]))[0]
    return {
        DENSE_VECTOR_NAME: dense,
        SPARSE_VECTOR_NAME: models.SparseVector(indices=list(sparse), values=list(sparse.values())),
    }


def _upsert_legacy(
    qdrant: QdrantClient,
    *,
    namespace: str,
    object_id: str,
    content: str,
    importance: int = 3,
    include_version: bool = True,
) -> str:
    point_id = str(uuid.uuid4())
    payload = _payload(
        namespace=namespace, object_id=object_id, content=content, importance=importance
    )
    if not include_version:
        payload.pop("version")
    qdrant.upsert(
        collection_name="musubi_episodic",
        points=[models.PointStruct(id=point_id, payload=payload, vector=_vectors(content))],
        wait=True,
    )
    return point_id


def _object_rows(qdrant: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    rows, _ = qdrant.scroll(
        collection_name="musubi_episodic",
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=20,
        with_payload=True,
        with_vectors=False,
    )
    return sorted((dict(row.payload or {}) for row in rows), key=lambda row: row["point_kind"])


def _seed_v2(
    publisher: ImmutableVectorPublisher,
    coordinator: LifecycleTransitionCoordinator,
    *,
    namespace: str = _NS,
) -> str:
    object_id = str(generate_ksuid())
    memory = EpisodicMemory(
        namespace=namespace,
        object_id=object_id,
        content="v2 embedded claim",
        summary="v2 summary",
        importance=3,
        tags=["existing", "remove-me"],
    )
    publisher.publish(
        coordinator,
        object_id=object_id,
        namespace=namespace,
        content_payload=memory.model_dump(mode="json"),
    )
    rows = _object_rows(publisher._client, object_id)
    assert [row["point_kind"] for row in rows] == ["anchor", "content"]
    return object_id


def test_same_object_id_in_two_namespaces_changes_only_authorized_namespace(
    client: TestClient, valid_token: str, qdrant: QdrantClient
) -> None:
    object_id = str(generate_ksuid())
    _upsert_legacy(qdrant, namespace=_NS, object_id=object_id, content="authorized", importance=3)
    other_point = _upsert_legacy(
        qdrant,
        namespace=_OTHER_NS,
        object_id=object_id,
        content="foreign namespace",
        importance=4,
    )

    response = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"importance": 9},
    )
    assert response.status_code == 200, response.text
    foreign = qdrant.retrieve(
        collection_name="musubi_episodic", ids=[other_point], with_payload=True
    )[0]
    assert foreign.payload is not None and foreign.payload["importance"] == 4


def test_v2_metadata_patch_changes_only_anchor_and_content_patch_is_typed_refusal(
    client: TestClient,
    valid_token: str,
    qdrant: QdrantClient,
    coordinator: LifecycleTransitionCoordinator,
    _immutable_publishers: tuple[Any, Any],
) -> None:
    publisher = _immutable_publishers[0]
    assert isinstance(publisher, ImmutableVectorPublisher)
    object_id = _seed_v2(publisher, coordinator)
    before = _object_rows(qdrant, object_id)

    metadata = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"importance": 8, "tags": ["replacement"]},
    )
    assert metadata.status_code == 200, metadata.text
    after_metadata = _object_rows(qdrant, object_id)
    assert after_metadata[0]["importance"] == 8
    assert after_metadata[0]["tags"] == ["replacement"]
    assert after_metadata[1] == before[1], "metadata PATCH must not touch immutable content"

    before_content = _object_rows(qdrant, object_id)
    refused = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"content": "RETRACTED: bounded tombstone pending escrow"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "CONFLICT"
    assert "#611" in refused.json()["error"]["detail"]
    assert _object_rows(qdrant, object_id) == before_content


def test_versionless_legacy_row_accepts_exactly_one_expected_version_zero_patch(
    client: TestClient, valid_token: str, qdrant: QdrantClient
) -> None:
    object_id = str(generate_ksuid())
    point_id = _upsert_legacy(
        qdrant,
        namespace=_NS,
        object_id=object_id,
        content="versionless legacy",
        include_version=False,
    )
    response = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"summary": "patched once"},
    )
    assert response.status_code == 200, response.text
    stored = qdrant.retrieve(collection_name="musubi_episodic", ids=[point_id], with_payload=True)[
        0
    ].payload
    assert stored is not None
    assert stored["version"] == 1
    assert "update_lease_token" not in stored


def test_legacy_content_patch_preserves_vectors_and_is_version_fenced(
    client: TestClient, valid_token: str, qdrant: QdrantClient
) -> None:
    object_id = str(generate_ksuid())
    point_id = _upsert_legacy(
        qdrant, namespace=_NS, object_id=object_id, content="original embedded claim"
    )
    before = qdrant.retrieve(collection_name="musubi_episodic", ids=[point_id], with_vectors=True)[
        0
    ]
    response = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"content": "RETRACTED: the original claim was false"},
    )
    assert response.status_code == 200, response.text
    after = qdrant.retrieve(
        collection_name="musubi_episodic", ids=[point_id], with_payload=True, with_vectors=True
    )[0]
    assert after.vector == before.vector, "retraction must preserve the original retrieval vector"
    assert after.payload is not None and after.payload["version"] == 2
    assert "update_lease_token" not in after.payload


def test_concurrent_same_version_patch_exactly_one_writer_succeeds_and_loser_gets_typed_conflict(
    qdrant: QdrantClient,
) -> None:
    from musubi.store.immutable_vectors import (
        NonEmbeddingPatchConflict,
        patch_non_embedding_payload,
    )

    object_id = str(generate_ksuid())
    _upsert_legacy(qdrant, namespace=_NS, object_id=object_id, content="race target")
    observed = _payload(namespace=_NS, object_id=object_id, content="race target")

    winner = patch_non_embedding_payload(
        qdrant,
        "musubi_episodic",
        namespace=_NS,
        object_id=object_id,
        observed_payload=observed,
        changes={"importance": 7},
        tag_mode="replace",
    )
    assert winner["importance"] == 7
    with pytest.raises(NonEmbeddingPatchConflict):
        patch_non_embedding_payload(
            qdrant,
            "musubi_episodic",
            namespace=_NS,
            object_id=object_id,
            observed_payload=observed,
            changes={"importance": 8},
            tag_mode="replace",
        )


def test_foreign_operation_token_at_same_next_version_cannot_falsely_confirm(
    client: TestClient,
    valid_token: str,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_id = str(generate_ksuid())
    point_id = _upsert_legacy(qdrant, namespace=_NS, object_id=object_id, content="token race")
    real_set_payload = qdrant.set_payload
    injected = False

    def inject_foreign_winner(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        payload = dict(kwargs.get("payload") or {})
        if not injected and payload.get("importance") == 8:
            injected = True
            real_set_payload(
                collection_name="musubi_episodic",
                payload={
                    "importance": 7,
                    "version": 2,
                    "update_lease_token": "done:foreign-operation",
                },
                points=[point_id],
                wait=True,
            )
        return real_set_payload(*args, **kwargs)

    monkeypatch.setattr(qdrant, "set_payload", inject_foreign_winner)
    response = client.patch(
        f"/v1/episodic/{object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"importance": 8},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    stored = qdrant.retrieve(collection_name="musubi_episodic", ids=[point_id], with_payload=True)[
        0
    ].payload
    assert stored is not None and stored["update_lease_token"] == "done:foreign-operation"


def test_replace_and_merge_tag_modes_and_summary_replacement_remain_distinct(
    client: TestClient, valid_token: str, episodic: EpisodicPlane
) -> None:
    saved = asyncio.run(
        episodic.create(
            EpisodicMemory(
                namespace=_NS,
                content="tag semantics",
                tags=["existing", "remove-me"],
                summary="old summary",
            )
        )
    )
    replaced = client.patch(
        f"/v1/episodic/{saved.object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"tags": ["replacement"], "summary": "new summary"},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["tags"] == ["replacement"]
    assert replaced.json()["summary"] == "new summary"

    merged, _event = asyncio.run(
        episodic.patch(
            namespace=_NS,
            object_id=saved.object_id,
            tags=["merged"],
            actor="test",
            reason="test explicit merge mode",
        )
    )
    assert set(merged.tags) == {"replacement", "merged"}


def test_val002_remains_clean_after_accepted_legacy_and_v2_mutations(
    client: TestClient,
    valid_token: str,
    qdrant: QdrantClient,
    coordinator: LifecycleTransitionCoordinator,
    _immutable_publishers: tuple[Any, Any],
) -> None:
    from musubi.cli.validate import _scan_collection

    legacy_id = str(generate_ksuid())
    _upsert_legacy(qdrant, namespace=_NS, object_id=legacy_id, content="legacy validation")
    v2_id = _seed_v2(_immutable_publishers[0], coordinator)
    for object_id in (legacy_id, v2_id):
        response = client.patch(
            f"/v1/episodic/{object_id}",
            headers=_headers(valid_token),
            params={"namespace": _NS},
            json={"importance": 6},
        )
        assert response.status_code == 200, response.text

    result = _scan_collection(qdrant, "episodic", "musubi_episodic", EpisodicMemory, 64)
    assert result.broken == []


def test_authorization_failure_reaches_neither_raw_read_nor_write(
    client: TestClient,
    out_of_scope_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("authorization failure reached storage")

    monkeypatch.setattr(episodic, "raw_payload", forbidden)
    monkeypatch.setattr(qdrant, "set_payload", forbidden)
    response = client.patch(
        f"/v1/episodic/{generate_ksuid()}",
        headers=_headers(out_of_scope_token),
        params={"namespace": _NS},
        json={"importance": 8},
    )
    assert response.status_code == 403, response.text
