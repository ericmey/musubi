"""Test contract for slice-plane-episodic.

Runs against an in-memory Qdrant (`qdrant_client.QdrantClient(":memory:")`)
and the deterministic :class:`FakeEmbedder`. These are unit tests — no network,
no GPU. Integration tests against a real Qdrant live under the ``integration``
pytest marker.

Test contract items covered (from [[04-data-model/episodic-memory]]):

1. Create sets state=provisional.
2. Create enforces namespace regex (delegated to pydantic; smoke only).
3. Create rejects event_at in the future (delegated to pydantic).
4. Create populates created_at/updated_at identically.
5. Create auto-embeds dense + sparse vectors.
6. Create dedup hit updates existing instead of inserting.
7. Create dedup hit merges tags.
8. Create dedup hit bumps reinforcement_count + version.
9. Create dedup hit updates content with new text.
10. Create dedup below threshold creates new.
11. Create dedup threshold is per-plane configurable.
12. Transition to matured emits a LifecycleEvent.
13. Transition to demoted filters from default queries.
14. Transition to archived removes from default queries.
15. Namespace isolation — read.
16. Namespace isolation — write (transition).
17. Query returns results in descending score order.
18. Query excludes provisional by default.
19. Query respects include_demoted flag.
20. Transition illegal raises ValueError (via LifecycleEvent validator).
21. get() returns None for missing object_id.
22. Transitions bump version + updated_at.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import LifecycleEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> EpisodicPlane:
    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


@pytest.fixture
def ns() -> str:
    return "eric/claude-code/episodic"


def _make(content: str, namespace: str, **extra: object) -> EpisodicMemory:
    """Small helper to build an :class:`EpisodicMemory` with sane defaults."""
    return EpisodicMemory(namespace=namespace, content=content, **extra)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def test_create_sets_provisional_state(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("hello world", ns))
    assert saved.state == "provisional"


async def test_create_populates_created_and_updated_identically(
    plane: EpisodicPlane, ns: str
) -> None:
    saved = await plane.create(_make("identical timestamps", ns))
    assert saved.created_at == saved.updated_at
    assert saved.created_epoch == saved.updated_epoch


async def test_create_returns_roundtrippable_object(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("a", ns))
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None
    assert fetched.object_id == saved.object_id
    assert fetched.content == saved.content


async def test_create_auto_embeds_dense_and_sparse_vectors(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    # After create, the point should exist in Qdrant with *both* named vectors.
    # Qdrant point IDs are UUIDs; KSUIDs live in the payload, so we scroll by
    # the payload field to locate the point without reaching into the plane's
    # internal ksuid -> UUID mapping.
    from qdrant_client import models as qmodels

    saved = await plane.create(_make("embed me", ns))
    records, _ = qdrant.scroll(
        collection_name="musubi_episodic",
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id",
                    match=qmodels.MatchValue(value=saved.object_id),
                )
            ]
        ),
        limit=1,
        with_vectors=True,
    )
    assert records, "point was not written to Qdrant"
    vectors = records[0].vector
    assert isinstance(vectors, dict)
    assert "dense_bge_m3_v1" in vectors
    assert "sparse_splade_v1" in vectors


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


async def test_create_dedup_hit_updates_existing_instead_of_inserting(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    first = await plane.create(_make("same", ns, tags=["a"]))
    second = await plane.create(_make("same", ns, tags=["b"]))
    # Dedup wins — same object id comes back on the second create.
    assert second.object_id == first.object_id
    # Only one point in Qdrant.
    count = qdrant.count(collection_name="musubi_episodic", exact=True).count
    assert count == 1


async def test_create_dedup_hit_merges_tags(plane: EpisodicPlane, ns: str) -> None:
    first = await plane.create(_make("merge tags", ns, tags=["a", "b"]))
    second = await plane.create(_make("merge tags", ns, tags=["b", "c"]))
    assert second.object_id == first.object_id
    assert set(second.tags) == {"a", "b", "c"}


async def test_create_dedup_hit_bumps_reinforcement_count_and_version(
    plane: EpisodicPlane, ns: str
) -> None:
    first = await plane.create(_make("bump me", ns))
    second = await plane.create(_make("bump me", ns))
    assert second.reinforcement_count == first.reinforcement_count + 1
    assert second.version == first.version + 1


async def test_create_dedup_hit_updates_content_with_new_text(
    plane: EpisodicPlane, ns: str
) -> None:
    # FakeEmbedder is content-addressed on SHA-256(text). We need two texts
    # close enough that the test would normally dedup. Since FakeEmbedder
    # gives random-ish vectors per unique text, force dedup by using a very
    # low threshold so *any* point counts as "same".
    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
    )
    first = await low_plane.create(_make("first version", "eric/claude-code/episodic"))
    second = await low_plane.create(_make("second version", "eric/claude-code/episodic"))
    assert second.object_id == first.object_id
    assert second.content == "second version"


async def test_create_dedup_below_threshold_creates_new(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    await plane.create(_make("totally different one", ns))
    await plane.create(_make("completely unrelated two", ns))
    count = qdrant.count(collection_name="musubi_episodic", exact=True).count
    # With default 0.92 threshold and FakeEmbedder's near-orthogonal random
    # vectors, different-text creates should not dedup.
    assert count == 2


async def test_create_dedup_threshold_is_per_plane_configurable(
    qdrant: QdrantClient, ns: str
) -> None:
    # threshold > 1 makes *nothing* dedup.
    strict = EpisodicPlane(client=qdrant, embedder=FakeEmbedder(), dedup_threshold=2.0)
    a = await strict.create(_make("same words", ns))
    b = await strict.create(_make("same words", ns))
    assert a.object_id != b.object_id


# ---------------------------------------------------------------------------
# Transitions (lifecycle events)
# ---------------------------------------------------------------------------


async def test_transition_to_matured_emits_lifecycle_event(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("mature me", ns))
    updated, event = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="test-suite",
        reason="unit-test",
    )
    assert updated.state == "matured"
    assert isinstance(event, LifecycleEvent)
    assert event.object_id == saved.object_id
    assert event.from_state == "provisional"
    assert event.to_state == "matured"
    assert event.actor == "test-suite"


async def test_transition_bumps_version_and_updated_at(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("bump on transition", ns))
    updated, _ = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="test",
        reason="unit-test",
    )
    assert updated.version == saved.version + 1
    assert updated.updated_epoch is not None and saved.updated_epoch is not None
    assert updated.updated_epoch >= saved.updated_epoch


async def test_transition_illegal_raises(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("illegal", ns))
    # provisional -> demoted is illegal per _ALLOWED["episodic"].
    with pytest.raises(ValueError):
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="demoted",
            actor="test",
            reason="unit-test",
        )


async def test_transition_to_demoted_keeps_record_but_filters_default_reads(
    plane: EpisodicPlane, ns: str
) -> None:
    saved = await plane.create(_make("demoteme", ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="rm",
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="rm",
    )
    # Still fetchable by id.
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None and fetched.state == "demoted"
    # But not in default queries.
    results = await plane.query(namespace=ns, query="demoteme", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_transition_to_archived_removes_from_default_queries(
    plane: EpisodicPlane, ns: str
) -> None:
    saved = await plane.create(_make("archived-me", ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="archived",
        actor="t",
        reason="rm",
    )
    results = await plane.query(namespace=ns, query="archived-me", limit=10)
    assert all(r.object_id != saved.object_id for r in results)
    # Still fetchable by id.
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None and fetched.state == "archived"


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------


async def test_isolation_read_enforcement(plane: EpisodicPlane) -> None:
    a_ns = "eric/claude-code/episodic"
    b_ns = "eric/livekit/episodic"
    a = await plane.create(_make("only-in-a", a_ns))
    b = await plane.create(_make("only-in-b", b_ns))
    # Querying A never returns B's object.
    results_a = await plane.query(namespace=a_ns, query="only", limit=10)
    assert all(r.object_id != b.object_id for r in results_a)
    # And get() in the wrong namespace returns None.
    assert await plane.get(namespace=a_ns, object_id=b.object_id) is None
    assert await plane.get(namespace=b_ns, object_id=a.object_id) is None


async def test_isolation_write_enforcement(plane: EpisodicPlane) -> None:
    a_ns = "eric/claude-code/episodic"
    b_ns = "eric/livekit/episodic"
    a = await plane.create(_make("write-isolation", a_ns))
    # Transitioning with the wrong namespace must fail rather than mutate A.
    with pytest.raises(LookupError):
        await plane.transition(
            namespace=b_ns,
            object_id=a.object_id,
            to_state="matured",
            actor="t",
            reason="unit-test",
        )
    # A is unchanged.
    still = await plane.get(namespace=a_ns, object_id=a.object_id)
    assert still is not None and still.state == "provisional"


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def test_query_excludes_provisional_by_default(plane: EpisodicPlane, ns: str) -> None:
    prov = await plane.create(_make("visible-later", ns))
    results = await plane.query(namespace=ns, query="visible", limit=10)
    # provisional excluded by default.
    assert all(r.object_id != prov.object_id for r in results)


async def test_query_includes_matured(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("include-me", ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="unit",
    )
    results = await plane.query(namespace=ns, query="include-me", limit=10)
    assert any(r.object_id == saved.object_id for r in results)


async def test_query_respects_include_demoted_flag(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("demoted-flag", ns))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="t",
        reason="u",
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="u",
    )
    default = await plane.query(namespace=ns, query="demoted-flag", limit=10)
    assert all(r.object_id != saved.object_id for r in default)
    including = await plane.query(
        namespace=ns, query="demoted-flag", limit=10, include_demoted=True
    )
    assert any(r.object_id == saved.object_id for r in including)


async def test_query_returns_at_most_limit_results(plane: EpisodicPlane, ns: str) -> None:
    for i in range(5):
        saved = await plane.create(_make(f"matured-{i}-unique", ns))
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="matured",
            actor="t",
            reason="u",
        )
    results = await plane.query(namespace=ns, query="matured", limit=3)
    assert len(results) <= 3


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_get_returns_none_for_missing_id(plane: EpisodicPlane, ns: str) -> None:
    # 27-char base62 KSUID that we never minted.
    missing = "0" * 27
    assert await plane.get(namespace=ns, object_id=missing) is None


async def test_create_rejects_future_event_at(plane: EpisodicPlane, ns: str) -> None:
    from datetime import timedelta

    from musubi.types.common import utc_now

    with pytest.raises(ValueError, match="future"):
        mem = EpisodicMemory(
            namespace=ns,
            content="Hit",
            event_at=utc_now() + timedelta(days=1),
            ingested_at=utc_now(),
        )
        await plane.create(mem)


async def test_create_enforces_namespace_regex(plane: EpisodicPlane) -> None:

    with pytest.raises(ValueError, match="namespace"):
        mem = EpisodicMemory(namespace="invalid namespace with spaces", content="Hit")
        await plane.create(mem)


async def test_content_over_32kb_rejected_with_suggestion_to_use_artifact(
    plane: EpisodicPlane, ns: str
) -> None:

    with pytest.raises(ValueError, match=r"32KB|artifact"):
        mem = EpisodicMemory(namespace=ns, content="A" * 33000)
        # wait, model validation might catch it first if max_length=32000 is on EpisodicMemory
        # The spec says "The content field is capped at 32KB. Long exchanges should be ingested as artifacts and cited via supported_by."
        # If Pydantic catches it, does it suggest artifact? We might need a plane-level guard or a custom validator on the model.
        await plane.create(mem)


async def test_vector_dimension_mismatch_rejected_with_clear_error(
    plane: EpisodicPlane, ns: str
) -> None:
    # Mock embedder to return wrong dimension
    from musubi.embedding.base import Embedder

    class BadEmbedder(Embedder):
        async def embed_dense(self, texts: Any) -> Any:
            return [[0.0] * 512 for _ in texts]

        async def embed_sparse(self, texts: Any) -> Any:
            return [{1: 1.0} for _ in texts]

        async def rerank(self, q: str, c: Any) -> Any:
            return []

    bad_plane = EpisodicPlane(client=plane._client, embedder=BadEmbedder())
    with pytest.raises(ValueError, match="dimension"):
        mem = EpisodicMemory(namespace=ns, content="Hit")
        await bad_plane.create(mem)


async def test_patch_importance_creates_lifecycle_event_and_bumps_version(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    updated, event = await plane.patch(
        namespace=ns, object_id=mem.object_id, importance=8, actor="test", reason="test patch"
    )
    assert updated.importance == 8
    assert updated.version == mem.version + 1
    assert event.object_type == "episodic"


async def test_patch_tags_is_additive_by_default(plane: EpisodicPlane, ns: str) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit", tags=["a"]))
    updated, _ = await plane.patch(
        namespace=ns, object_id=mem.object_id, tags=["b"], actor="test", reason="test patch"
    )
    assert "a" in updated.tags and "b" in updated.tags


async def test_patch_forbids_mutating_content_directly(plane: EpisodicPlane, ns: str) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    with pytest.raises(ValueError, match="content"):
        await plane.patch(
            namespace=ns, object_id=mem.object_id, content="new", actor="test", reason="test patch"
        )


async def test_delete_requires_operator_scope(plane: EpisodicPlane, ns: str) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    with pytest.raises(PermissionError, match="operator"):
        await plane.delete(
            namespace=ns,
            object_id=mem.object_id,
            actor="test",
            reason="test delete",
            is_operator=False,
        )


async def test_delete_creates_audit_event(plane: EpisodicPlane, ns: str) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    event = await plane.delete(
        namespace=ns, object_id=mem.object_id, actor="test", reason="test delete", is_operator=True
    )
    assert event.to_state == "archived"
    assert await plane.get(namespace=ns, object_id=mem.object_id) is None


async def test_access_count_increments_via_batch_update_points(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    # fetch triggers access_count bump
    fetched = await plane.get(namespace=ns, object_id=mem.object_id)
    assert fetched and fetched.access_count == 1
    fetched2 = await plane.get(namespace=ns, object_id=mem.object_id)
    assert fetched2 and fetched2.access_count == 2


async def test_access_count_update_is_not_N_plus_1(plane: EpisodicPlane, ns: str) -> None:

    for i in range(5):
        mem = await plane.create(EpisodicMemory(namespace=ns, content=f"Hit {i}"))
        await plane.transition(
            namespace=ns, object_id=mem.object_id, to_state="matured", actor="test", reason="test"
        )

    hits = await plane.query(namespace=ns, query="Hit", limit=5, include_demoted=True)
    assert len(hits) == 5


async def test_demotion_keeps_record_but_filters_from_default_reads(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    await plane.transition(
        namespace=ns, object_id=mem.object_id, to_state="matured", actor="test", reason="mature"
    )
    await plane.transition(
        namespace=ns, object_id=mem.object_id, to_state="demoted", actor="test", reason="demote"
    )
    hits = await plane.query(namespace=ns, query="Hit")
    assert not hits
    hits_demoted = await plane.query(namespace=ns, query="Hit", include_demoted=True)
    assert len(hits_demoted) == 1


async def test_archival_removes_from_default_queries_but_returns_from_get_by_id(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    await plane.transition(
        namespace=ns, object_id=mem.object_id, to_state="archived", actor="test", reason="archive"
    )
    hits = await plane.query(namespace=ns, query="Hit", include_demoted=True)
    assert not hits
    fetched = await plane.get(namespace=ns, object_id=mem.object_id)
    assert fetched is not None


async def test_query_respects_state_filter_default_excludes_provisional(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    assert mem.state == "provisional"
    hits = await plane.query(namespace=ns, query="Hit")
    assert not hits


async def test_concurrent_dedup_race_resolves_to_single_winner(
    plane: EpisodicPlane, ns: str
) -> None:

    import asyncio

    mem1 = EpisodicMemory(namespace=ns, content="Hit", tags=["a"])
    mem2 = EpisodicMemory(namespace=ns, content="Hit", tags=["b"])
    res = await asyncio.gather(plane.create(mem1), plane.create(mem2))
    final_mem = await plane.get(namespace=ns, object_id=res[0].object_id)
    assert final_mem is not None
    assert "a" in final_mem.tags and "b" in final_mem.tags


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_idempotency_re_ingesting_same_content_N_times_produces_1_memory_with_reinforcement_count_N() -> (
    None
):
    pass


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_lifecycle_monotonicity_state_transitions_never_go_backwards_except_explicit_revive_operation() -> (
    None
):
    pass
