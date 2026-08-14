"""Default-gate regressions for DATA-001 physical layout-field isolation (#697)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.immutable_vectors import (
    ImmutableVectorPublisher,
    anchor_point_id,
    read_anchor,
)
from musubi.store.names import collection_for_plane
from musubi.store.specs import LAYOUT_ONLY_FIELDS

_NAMESPACE = "eric/data001-layout-leak/episodic"
_ANCHOR_LAYOUT_FIELDS = {
    "point_kind",
    "live_point",
    "pointer_version",
    "committed_operation_id",
    "vector_layout_version",
}
_CONTENT_ONLY_LAYOUT_FIELDS = LAYOUT_ONLY_FIELDS - _ANCHOR_LAYOUT_FIELDS


def _harness(tmp_path: Path) -> tuple[QdrantClient, str, LifecycleTransitionCoordinator, Any]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    collection = collection_for_plane("episodic")
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "coord.db")
    publisher = ImmutableVectorPublisher(
        client=client,
        embedder=FakeEmbedder(),
        collection=collection,
    )
    publisher.register(coordinator)
    return client, collection, coordinator, publisher


def _raw_anchor(client: QdrantClient, collection: str, object_id: str) -> dict[str, Any]:
    points = client.retrieve(
        collection_name=collection,
        ids=[anchor_point_id(_NAMESPACE, object_id)],
        with_payload=True,
    )
    assert len(points) == 1 and points[0].payload is not None
    return dict(points[0].payload)


def _raw_content(client: QdrantClient, collection: str, object_id: str) -> dict[str, Any]:
    anchor = read_anchor(
        client,
        collection,
        namespace=_NAMESPACE,
        object_id=object_id,
    )
    assert anchor is not None and anchor.live_point is not None
    points = client.retrieve(
        collection_name=collection,
        ids=[anchor.live_point],
        with_payload=True,
    )
    assert len(points) == 1 and points[0].payload is not None
    return dict(points[0].payload)


def test_payload_only_rebase_strips_layout_fields_from_anchor(tmp_path: Path) -> None:
    client, collection, coordinator, publisher = _harness(tmp_path)
    try:
        publisher.publish(
            coordinator,
            object_id="payload-only",
            namespace=_NAMESPACE,
            content_payload={"content": "short", "summary": "stable", "tags": ["a"]},
        )
        content_before = _raw_content(client, collection, "payload-only")

        asyncio.run(
            publisher.reinforce_publish(
                coordinator,
                object_id="payload-only",
                namespace=_NAMESPACE,
                new_memory={"content": "a much longer body", "tags": ["b"]},
                merge_strategy="longer-wins",
            )
        )
        publisher.publish(
            coordinator,
            object_id="payload-only",
            namespace=_NAMESPACE,
            content_payload={"generation": "caller", "owner_token": "caller"},
        )

        anchor = _raw_anchor(client, collection, "payload-only")
        assert set(anchor).isdisjoint(_CONTENT_ONLY_LAYOUT_FIELDS)
        assert _raw_content(client, collection, "payload-only") == content_before
    finally:
        client.close()


def test_vector_change_rebase_preserves_strict_physical_envelopes(tmp_path: Path) -> None:
    client, collection, coordinator, publisher = _harness(tmp_path)
    try:
        publisher.curated_publish(
            coordinator,
            object_id="vector-change",
            namespace=_NAMESPACE,
            set_fields={"title": "before", "summary": "stable", "content": "body"},
        )
        publisher.curated_publish(
            coordinator,
            object_id="vector-change",
            namespace=_NAMESPACE,
            set_fields={
                "title": "after",
                "generation": "caller",
                "owner_token": "caller",
            },
        )

        anchor = _raw_anchor(client, collection, "vector-change")
        assert set(anchor).isdisjoint(_CONTENT_ONLY_LAYOUT_FIELDS)
        content = _raw_content(client, collection, "vector-change")
        assert set(content) == {
            "object_id",
            "namespace",
            "point_kind",
            "generation",
            "owner_token",
            "title",
            "content",
            "summary",
        }
        assert content["generation"] != "caller"
        assert content["owner_token"] != "caller"
    finally:
        client.close()
