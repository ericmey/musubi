"""Default-gate regressions for DATA-001 physical layout-field isolation (#697)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.immutable_vectors import (
    ImmutableVectorPublisher,
    read_anchor,
)
from musubi.store.names import collection_for_plane
from musubi.store.specs import LAYOUT_ONLY_FIELDS
from tests.support.identity_seed import (
    FIXTURE_KSUIDS,
    seed_curated_v2_identity_via_migration,
    seed_v2_identity_via_migration,
)

_NAMESPACE = "eric/data001-layout-leak/episodic"
_ANCHOR_LAYOUT_FIELDS = {
    "point_kind",
    "live_point",
    "pointer_version",
    "committed_operation_id",
    "vector_layout_version",
}
_CONTENT_ONLY_LAYOUT_FIELDS = LAYOUT_ONLY_FIELDS - _ANCHOR_LAYOUT_FIELDS


def _harness(
    tmp_path: Path, plane: str = "episodic"
) -> tuple[QdrantClient, str, LifecycleTransitionCoordinator, Any]:
    """Publisher bound to the collection its content actually lives in.

    The curated cell below used to run `curated_publish` against an EPISODIC-collection
    publisher, which put curated rows in a collection production never puts them in --
    unreal state of the same family as the impossible object_ids this file also carried.
    `CuratedPlane` hardcodes `collection_for_plane("curated")`, so a curated fixture
    cannot be seeded the production way against an episodic collection at all.
    """
    client = QdrantClient(":memory:")
    bootstrap(client)
    collection = collection_for_plane(plane)
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "coord.db")
    publisher = ImmutableVectorPublisher(
        client=client,
        embedder=FakeEmbedder(),
        collection=collection,
    )
    publisher.register(coordinator)
    return client, collection, coordinator, publisher


def _raw_anchor(client: QdrantClient, collection: str, object_id: str) -> dict[str, Any]:
    """Find the anchor the way PRODUCTION finds it: by payload filter, not by point id.

    An anchor lives in one of TWO id spaces depending on how it came to exist:
    `anchor_point_id(namespace, object_id)` for one created fresh, and the ORIGINAL
    LEGACY POINT ID for one converted in place by the migration path. The codebase
    already knows this -- `immutable_vectors.py:772` retrieves both, and the comment at
    :751 names them -- and `read_anchor` sidesteps it entirely with a filter.

    This helper looked up the deterministic id only. That worked while the absent-publish
    create seeded these tests, because that path put the anchor there. Against a
    migration-seeded row it finds nothing, which is a fact about the helper rather than
    about the anchor (musubi#732 round 29).
    """
    recs, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
                models.FieldCondition(key="namespace", match=models.MatchValue(value=_NAMESPACE)),
                models.FieldCondition(key="point_kind", match=models.MatchValue(value="anchor")),
            ]
        ),
        limit=2,
        with_payload=True,
    )
    assert len(recs) == 1 and recs[0].payload is not None, (
        f"expected exactly one anchor for {object_id!r}, found {len(recs)}"
    )
    return dict(recs[0].payload)


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
        # LAYOUT: v2 anchor + content. This cell asserts on the ANCHOR envelope, so it
        # needs the v2 topology -- reached through the production migration path, not the
        # absent-publish create that round 29 removed.
        object_id = FIXTURE_KSUIDS["payload-only"]
        seed_v2_identity_via_migration(
            client,
            coordinator,
            publisher,
            namespace=_NAMESPACE,
            object_id=object_id,
            content="short",
            tags=["a"],
        )
        content_before = _raw_content(client, collection, object_id)

        asyncio.run(
            publisher.reinforce_publish(
                coordinator,
                object_id=object_id,
                namespace=_NAMESPACE,
                new_memory={"content": "a much longer body", "tags": ["b"]},
                merge_strategy="longer-wins",
            )
        )
        publisher.publish(
            coordinator,
            object_id=object_id,
            namespace=_NAMESPACE,
            content_payload={"generation": "caller", "owner_token": "caller"},
        )

        anchor = _raw_anchor(client, collection, object_id)
        assert set(anchor).isdisjoint(_CONTENT_ONLY_LAYOUT_FIELDS)
        assert _raw_content(client, collection, object_id) == content_before
    finally:
        client.close()


def test_vector_change_rebase_preserves_strict_physical_envelopes(tmp_path: Path) -> None:
    client, collection, coordinator, publisher = _harness(tmp_path, plane="curated")
    try:
        # LAYOUT: curated v2 anchor + content, via the curated migration path.
        object_id = FIXTURE_KSUIDS["vector-change"]
        seed_curated_v2_identity_via_migration(
            client,
            coordinator,
            publisher,
            namespace=_NAMESPACE,
            object_id=object_id,
            title="before",
            content="body",
        )
        publisher.curated_publish(
            coordinator,
            object_id=object_id,
            namespace=_NAMESPACE,
            set_fields={
                "title": "after",
                "generation": "caller",
                "owner_token": "caller",
            },
        )

        anchor = _raw_anchor(client, collection, object_id)
        assert set(anchor).isdisjoint(_CONTENT_ONLY_LAYOUT_FIELDS)
        content = _raw_content(client, collection, object_id)
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
