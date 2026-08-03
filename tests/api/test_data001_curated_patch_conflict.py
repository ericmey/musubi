"""DATA-001 #637: curated HTTP PATCH is one-shot while plane PATCH may retry."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

import musubi.api.routers.writes_curated as writes_curated
from musubi.api.patch_guard import assert_readable_after_patch
from musubi.planes.curated import CuratedPlane
from musubi.store.immutable_vectors import patch_non_embedding_payload
from musubi.store.names import collection_for_plane
from musubi.types.curated import CuratedKnowledge

_NS = "eric/claude-code/curated"
_COLLECTION = collection_for_plane("curated")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _memory(
    *,
    content: str,
    object_id: str | None = None,
    title: str = "Curated PATCH target",
    tags: list[str] | None = None,
) -> CuratedKnowledge:
    values: dict[str, Any] = {
        "namespace": _NS,
        "title": title,
        "content": content,
        "vault_path": "curated/eric/data001-patch-conflict.md",
        "body_hash": hashlib.sha256(content.encode()).hexdigest(),
        "tags": tags or ["existing", "remove-me"],
        "topics": ["old-topic"],
        "importance": 3,
    }
    if object_id is not None:
        values["object_id"] = object_id
    return CuratedKnowledge.model_validate(values)


def _object_rows(qdrant: QdrantClient, object_id: str) -> list[dict[str, Any]]:
    rows, _ = qdrant.scroll(
        collection_name=_COLLECTION,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=20,
        with_payload=True,
        with_vectors=False,
    )
    return sorted((dict(row.payload or {}) for row in rows), key=lambda row: row["point_kind"])


def test_curated_http_patch_same_version_loser_returns_typed_conflict_without_retry(
    client: TestClient,
    valid_token: str,
    curated: CuratedPlane,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = asyncio.run(curated.create(_memory(content="same-version race")))
    observed = asyncio.run(curated.raw_payload(namespace=_NS, object_id=str(saved.object_id)))
    assert observed is not None and observed["version"] == 1

    injected = False

    def inject_same_version_winner(*args: Any, **kwargs: Any) -> None:
        nonlocal injected
        assert_readable_after_patch(*args, **kwargs)
        if injected:
            return
        injected = True
        winner = patch_non_embedding_payload(
            qdrant,
            _COLLECTION,
            namespace=_NS,
            object_id=str(saved.object_id),
            observed_payload=observed,
            changes={"importance": 7},
            tag_mode="replace",
        )
        assert winner["importance"] == 7 and winner["version"] == 2

    monkeypatch.setattr(writes_curated, "assert_readable_after_patch", inject_same_version_winner)
    response = client.patch(
        f"/v1/curated/{saved.object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"importance": 8},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "lost its observed-version fence" in response.json()["error"]["detail"]
    stored = asyncio.run(curated.get(namespace=_NS, object_id=str(saved.object_id)))
    assert stored is not None
    assert stored.importance == 7, "the HTTP loser silently replayed over the real winner"
    assert stored.version == 2, "a stale HTTP request must not create a later version"


def test_curated_http_patch_preserves_replace_tag_topic_and_importance_semantics(
    client: TestClient, valid_token: str, curated: CuratedPlane
) -> None:
    saved = asyncio.run(curated.create(_memory(content="metadata semantics")))
    response = client.patch(
        f"/v1/curated/{saved.object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"tags": ["replacement"], "topics": ["new-topic"], "importance": 9},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tags"] == ["replacement"]
    assert body["topics"] == ["new-topic"]
    assert body["importance"] == 9
    assert body["version"] == saved.version + 1


def test_curated_http_patch_v2_changes_only_anchor_and_preserves_generation_binding(
    client: TestClient,
    valid_token: str,
    curated: CuratedPlane,
    qdrant: QdrantClient,
) -> None:
    first = asyncio.run(curated.create(_memory(content="v1 curated text")))
    asyncio.run(
        curated.create(
            _memory(
                content="v2 curated text is deliberately different",
                object_id=str(first.object_id),
                title="Curated PATCH target v2",
            )
        )
    )
    before = _object_rows(qdrant, str(first.object_id))
    assert [row["point_kind"] for row in before] == ["anchor", "content"]

    response = client.patch(
        f"/v1/curated/{first.object_id}",
        headers=_headers(valid_token),
        params={"namespace": _NS},
        json={"tags": ["v2-replacement"], "importance": 8},
    )
    assert response.status_code == 200, response.text

    after = _object_rows(qdrant, str(first.object_id))
    assert after[0]["tags"] == ["v2-replacement"]
    assert after[0]["importance"] == 8
    assert after[0]["committed_operation_id"] == before[0]["committed_operation_id"]
    assert after[1] == before[1], "metadata PATCH must not touch immutable curated content"
    assert "update_lease_token" not in after[0]
