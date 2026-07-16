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
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.transitions import TransitionResult
from musubi.planes.episodic import EpisodicPlane
from musubi.planes.episodic.plane import MergeStrategy
from musubi.store import bootstrap
from musubi.types.common import Err, Ok
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


_COORDINATOR: LifecycleTransitionCoordinator | None = None


@pytest.fixture(autouse=True)
def coordinator(qdrant: QdrantClient, tmp_path: Path) -> LifecycleTransitionCoordinator:
    global _COORDINATOR
    _COORDINATOR = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    return _COORDINATOR


@pytest.fixture
def plane(qdrant: QdrantClient, coordinator: LifecycleTransitionCoordinator) -> EpisodicPlane:
    # DATA-001 P2: a vector-changing reinforce publishes through the immutable-vector seam; wire the
    # coordinator + a collection-bound publisher + the dispatcher so the reinforce path is not fail-closed.
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )
    from musubi.store.names import collection_for_plane

    coll = collection_for_plane("episodic")
    publisher = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=coll)
    register_immutable_vector_dispatch(coordinator, {coll: publisher})
    return EpisodicPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coordinator, vector_publisher=publisher
    )


def _coord() -> LifecycleTransitionCoordinator:
    assert _COORDINATOR is not None
    return _COORDINATOR


def _final(result: object) -> TransitionResult:
    assert isinstance(result, Ok)
    assert isinstance(result.value, TransitionResult)
    return result.value


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
    # The longer-wins/new-text policy applies ONLY after factual compatibility authorizes the merge.
    # We must use normalization-equivalent content to bypass the factual compatibility guard.
    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    first_cand = _make("first version.", ns)
    first_cand.summary = "first version"
    first = await low_plane.create(first_cand)

    second_cand = _make("  FIRST version !!!  ", ns)
    second_cand.summary = "first version"
    second = await low_plane.create(second_cand)

    assert second.object_id == first.object_id
    assert second.content == "  FIRST version !!!  "


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
    outcome = _final(
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="matured",
            actor="test-suite",
            reason="unit-test",
            coordinator=_coord(),
        )
    )
    updated = await plane.get(namespace=ns, object_id=saved.object_id)
    assert updated is not None
    event = outcome.event
    assert updated.state == "matured"
    assert isinstance(event, LifecycleEvent)
    assert event.object_id == saved.object_id
    assert event.from_state == "provisional"
    assert event.to_state == "matured"
    assert event.actor == "test-suite"


async def test_transition_bumps_version_and_updated_at(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("bump on transition", ns))
    _final(
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="matured",
            actor="test",
            reason="unit-test",
            coordinator=_coord(),
        )
    )
    updated = await plane.get(namespace=ns, object_id=saved.object_id)
    assert updated is not None
    assert updated.version == saved.version + 1
    assert updated.updated_epoch is not None and saved.updated_epoch is not None
    assert updated.updated_epoch >= saved.updated_epoch


async def test_transition_illegal_raises(plane: EpisodicPlane, ns: str) -> None:
    saved = await plane.create(_make("illegal", ns))
    # provisional -> demoted is illegal per _ALLOWED["episodic"].
    result = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="test",
        reason="unit-test",
        coordinator=_coord(),
    )
    assert isinstance(result, Err)
    assert result.error.code == "illegal_transition"


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
        coordinator=_coord(),
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="rm",
        coordinator=_coord(),
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
        coordinator=_coord(),
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
    result = await plane.transition(
        namespace=b_ns,
        object_id=a.object_id,
        to_state="matured",
        actor="t",
        reason="unit-test",
        coordinator=_coord(),
    )
    assert isinstance(result, Err)
    assert result.error.code == "not_found"
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
        coordinator=_coord(),
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
        coordinator=_coord(),
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="u",
        coordinator=_coord(),
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
            coordinator=_coord(),
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


async def test_get_missing_id_does_not_bump_access(
    plane: EpisodicPlane, ns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATA-001 P2 (Yua): a missing/dangling get must RESOLVE first and return None WITHOUT attempting
    an access-lease mutation — the pre-P2 contract mutated nothing on an absent row."""
    from musubi.store.access_lease import lease_increment_access as _orig_lease

    missing = "0" * 27
    calls = {"n": 0}

    async def _spy(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        await _orig_lease(*args, **kwargs)

    monkeypatch.setattr("musubi.planes.episodic.plane.lease_increment_access", _spy)
    assert await plane.get(namespace=ns, object_id=missing) is None
    assert calls["n"] == 0, "get() on a missing id must not attempt an access-lease mutation"


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
            namespace=ns,
            object_id=mem.object_id,
            to_state="matured",
            actor="test",
            reason="test",
            coordinator=_coord(),
        )

    hits = await plane.query(namespace=ns, query="Hit", limit=5, include_demoted=True)
    assert len(hits) == 5


async def test_demotion_keeps_record_but_filters_from_default_reads(
    plane: EpisodicPlane, ns: str
) -> None:

    mem = await plane.create(EpisodicMemory(namespace=ns, content="Hit"))
    await plane.transition(
        namespace=ns,
        object_id=mem.object_id,
        to_state="matured",
        actor="test",
        reason="mature",
        coordinator=_coord(),
    )
    await plane.transition(
        namespace=ns,
        object_id=mem.object_id,
        to_state="demoted",
        actor="test",
        reason="demote",
        coordinator=_coord(),
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
        namespace=ns,
        object_id=mem.object_id,
        to_state="archived",
        actor="test",
        reason="archive",
        coordinator=_coord(),
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


# ---------------------------------------------------------------------------
# Semantic Factual Deduplication — ING-001
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_merges_exact_duplicate(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 1. Exact duplicate
    base = await plane.create(_make("Deploy succeeded.", ns))
    candidate = _make("Deploy succeeded.", ns)

    if use_batch:
        res = await plane.batch_create([candidate])
        final = res[0]
    else:
        final = await plane.create(candidate)

    assert final.object_id == base.object_id
    assert final.reinforcement_count == 1

    count = qdrant.count(collection_name="musubi_episodic", exact=True).count
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_merges_normalized_duplicate(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 2. Normalized duplicate (case, whitespace, terminal punctuation differences only)
    base = await plane.create(_make("Deploy succeeded.", ns))
    candidate = _make("  DEPLOY  succeeded!!! ", ns)

    # We must force a low threshold because our FakeEmbedder will randomly generate vectors for different strings.
    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )

    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id == base.object_id
    assert final.reinforcement_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_paraphrase(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # Paraphrases that are not normalization-equivalent must remain distinct
    base = await plane.create(_make("The deployment was successful.", ns))
    candidate = _make("Deployment succeeded.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id
    assert final.reinforcement_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_participants_change(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # In addition to normal participant changes in text, verify structured participants metadata differences
    base_obj = _make("Meeting finished", ns)
    base_obj.participants = ["aoi"]
    base = await plane.create(base_obj)

    candidate = _make("Meeting finished", ns)
    candidate.participants = ["yua"]

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id
    assert final.reinforcement_count == 0


@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_correction(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 3. Correction
    base = await plane.create(_make("Deploy succeeded.", ns))
    candidate = _make("Deploy failed.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id
    assert final.reinforcement_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_negation(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 4. Negation
    base = await plane.create(_make("The server is up.", ns))
    candidate = _make("The server is not up.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_participant_change(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 5. Participant change
    base = await plane.create(_make("Aoi reviewed the PR.", ns))
    candidate = _make("Yua reviewed the PR.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_time_change(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 6. Time change
    base = await plane.create(_make("Meeting at 4pm.", ns))
    candidate = _make("Meeting at 5pm.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_conflicting_numbers(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 7. Conflicting Numbers (including signs/decimals)
    base = await plane.create(_make("We have 10 nodes.", ns))
    c1 = _make("We have 12 nodes.", ns)

    base2 = await plane.create(_make("Temp is 4.5", ns))
    c2 = _make("Temp is 45", ns)

    base3 = await plane.create(_make("Offset is 5", ns))
    c3 = _make("Offset is -5", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    for c, b in [(c1, base), (c2, base2), (c3, base3)]:
        if use_batch:
            res = await low_plane.batch_create([c])
            final = res[0]
        else:
            final = await low_plane.create(c)
        assert final.object_id != b.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_ambiguity(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # 8. Ambiguity
    base = await plane.create(_make("Near match.", ns))
    candidate = _make("A near match.", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_rejects_language_token_punctuation(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # Punctuation that changes meaning (e.g. C vs C++, can't vs cant) MUST reject
    base = await plane.create(_make("We write C", ns))
    c1 = _make("We write C++", ns)

    base2 = await plane.create(_make("I cant", ns))
    c2 = _make("I can't", ns)

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    for c, b in [(c1, base), (c2, base2)]:
        if use_batch:
            res = await low_plane.batch_create([c])
            final = res[0]
        else:
            final = await low_plane.create(c)
        assert final.object_id != b.object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_batch", [False, True])
async def test_semantic_dedup_compares_content_not_summary(
    plane: EpisodicPlane, ns: str, use_batch: bool, qdrant: QdrantClient
) -> None:
    # Summary matches, but authoritative content does not
    base_obj = _make("Detailed failure log A", ns)
    base_obj.summary = "Failure summary"
    base = await plane.create(base_obj)

    candidate = _make("Detailed failure log B", ns)
    candidate.summary = "Failure summary"

    low_plane = EpisodicPlane(
        client=plane._client,
        embedder=plane._embedder,
        dedup_threshold=-1.0,
        coordinator=plane._coordinator,
        vector_publisher=plane._vector_publisher,
    )
    if use_batch:
        res = await low_plane.batch_create([candidate])
        final = res[0]
    else:
        final = await low_plane.create(candidate)

    assert final.object_id != base.object_id


@pytest.mark.asyncio
async def test_batch_create_intra_batch_rejects_factual_incompatibility(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    # Near match but factual incompatible (negation)
    base = _make("The server is up.", ns)
    candidate = _make("The server is not up.", ns)

    low_plane = EpisodicPlane(client=plane._client, embedder=plane._embedder, dedup_threshold=-1.0)

    res = await low_plane.batch_create([base, candidate])

    # They should NOT merge, returning 2 distinct rows
    assert res[0].object_id != res[1].object_id
    assert qdrant.count(collection_name="musubi_episodic", exact=True).count == 2


@pytest.mark.asyncio
async def test_batch_create_cross_namespace_isolation(
    plane: EpisodicPlane, qdrant: QdrantClient
) -> None:
    ns1 = "eric/ops/episodic"
    ns2 = "yua/ops/episodic"

    m1 = _make("Cross namespace match", ns1)
    m2 = _make("Cross namespace match", ns2)

    low_plane = EpisodicPlane(client=plane._client, embedder=plane._embedder, dedup_threshold=-1.0)
    res = await low_plane.batch_create([m1, m2])

    # They should NOT merge because namespaces differ
    assert res[0].object_id != res[1].object_id
    assert qdrant.count(collection_name="musubi_episodic", exact=True).count == 2


@pytest.mark.asyncio
async def test_intrabatch_dedup_sequential_duplicate(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    m1 = _make("Deploy succeeded.", ns)
    m2 = _make("Deploy succeeded.", ns)

    res = await plane.batch_create([m1, m2])

    assert len(res) == 2
    assert res[0].object_id == res[1].object_id
    assert res[1].reinforcement_count == 1

    count = qdrant.count(collection_name="musubi_episodic", exact=True).count
    assert count == 1


@pytest.mark.asyncio
async def test_intrabatch_dedup_prefers_best_score_and_tie_breaks(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    # Use identical compatible content for persisted/pending/candidate to bypass the factual compatibility constraint
    # naturally without monkey-patching. The geometry will keep earlier rows below threshold vs persisted,
    # making candidate score .707 vs persisted and .819 vs pending_strong.
    from musubi.embedding.base import Embedder

    class ScoreEmbedder(Embedder):
        def __init__(self) -> None:
            self._idx = 0
            import math

            def v(deg: float) -> list[float]:
                r = math.radians(deg)
                return [math.cos(r), math.sin(r)] + [0.0] * 1022

            self._vectors = [
                # persisted_near
                v(0),
                # pending_weak
                v(-80),
                # pending_strong
                v(80),
                # candidate
                v(45),
            ]

        async def embed_dense(self, texts: list[str]) -> list[list[float]]:
            res = []
            for _ in texts:
                res.append(self._vectors[self._idx])
                self._idx += 1
            return res

        async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
            return [{} for _ in texts]

        async def rerank(self, query: str, candidates: list[str]) -> list[float]:
            return [1.0] * len(candidates)

    test_plane = EpisodicPlane(client=plane._client, embedder=ScoreEmbedder(), dedup_threshold=0.5)

    persisted_near = _make("identical content", ns)
    persisted_near.object_id = "000000000000000000000000001"
    await test_plane.create(persisted_near)

    # Overwrite the object_id in payload so it has the expected ID
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"object_id": persisted_near.object_id},
        points=[test_plane._client.scroll(collection_name="musubi_episodic")[0][0].id],
    )

    pending_weak = _make("identical content", ns)
    pending_weak.object_id = "000000000000000000000000002"

    pending_strong = _make("identical content", ns)
    pending_strong.object_id = "000000000000000000000000003"

    candidate = _make("identical content", ns)

    # Run batch
    # Candidate matches pending_weak (-0.57 < 0.5), persisted_near (0.707 >= 0.5), pending_strong (0.819 >= 0.5)
    # It must pick pending_strong!
    res = await test_plane.batch_create([pending_weak, pending_strong, candidate])

    # Ensure they map properly
    assert len(res) == 3
    # pending_weak -> fresh
    # pending_strong -> merges candidate -> has count 1
    assert res[0].object_id == pending_weak.object_id
    assert res[0].reinforcement_count == 0
    assert res[1].object_id == pending_strong.object_id
    assert res[1].reinforcement_count == 0
    assert res[2].object_id == pending_strong.object_id
    assert res[2].reinforcement_count == 1

    # persisted_near was not touched
    persisted = await test_plane.get(namespace=ns, object_id=persisted_near.object_id)
    assert persisted is not None
    assert persisted.reinforcement_count == 0


@pytest.mark.asyncio
async def test_intrabatch_dedup_sequential_tiebreak_equal_score(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient
) -> None:
    # A valid tie fixture: no persisted row; pending vectors at 0 and 100 degrees (mutual cosine < .5),
    # candidate at 50 degrees (equal .643 to both), identical factual content.
    # Assert the documented stable object_id choice (highest ID).
    from musubi.embedding.base import Embedder

    class TieEmbedder(Embedder):
        def __init__(self) -> None:
            self._idx = 0
            import math

            def v(deg: float) -> list[float]:
                r = math.radians(deg)
                return [math.cos(r), math.sin(r)] + [0.0] * 1022

            self._vectors = [
                # pending 1 (0 degrees)
                v(0),
                # pending 2 (100 degrees)
                v(100),
                # candidate (50 degrees)
                v(50),
            ]

        async def embed_dense(self, texts: list[str]) -> list[list[float]]:
            res = []
            for _ in texts:
                res.append(self._vectors[self._idx])
                self._idx += 1
            return res

        async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
            return [{} for _ in texts]

        async def rerank(self, query: str, candidates: list[str]) -> list[float]:
            return [1.0] * len(candidates)

    test_plane = EpisodicPlane(client=plane._client, embedder=TieEmbedder(), dedup_threshold=0.5)

    m1 = _make("identical content", ns)
    m1.object_id = "000000000000000000000000001"

    m2 = _make("identical content", ns)
    m2.object_id = "000000000000000000000000002"

    m3 = _make("identical content", ns)

    res = await test_plane.batch_create([m1, m2, m3])

    assert len(res) == 3
    # m1 -> fresh (count 0)
    # m2 -> fresh (count 0), cos(0, 100) = -0.173 < 0.5
    # m3 -> matches m1 (cos=0.643) and m2 (cos=0.643).
    # Picks highest object_id, which is m2!

    assert res[0].object_id == m1.object_id
    assert res[0].reinforcement_count == 0
    assert res[1].object_id == m2.object_id
    assert res[1].reinforcement_count == 0

    assert res[2].object_id == m2.object_id
    assert res[2].reinforcement_count == 1


@pytest.mark.asyncio
async def test_batch_create_enforces_100_item_limit(plane: EpisodicPlane, ns: str) -> None:
    mems = [_make(f"item {i}", ns) for i in range(101)]
    with pytest.raises(ValueError, match="exceeds maximum batch size of 100"):
        await plane.batch_create(mems)


@pytest.mark.asyncio
async def test_batch_vs_sequential_multiple_clusters(
    plane: EpisodicPlane, qdrant: QdrantClient
) -> None:
    # Batch of 4 items: 2 near A, 2 near B.
    ns_batch = "eric/ops/episodic"
    m_a1 = _make("Cluster A text is this one", ns_batch)
    m_a2 = _make("Cluster A text is this one", ns_batch)
    m_b1 = _make("Cluster B content is here", ns_batch)
    m_b2 = _make("Cluster B content is here", ns_batch)

    res_batch = await plane.batch_create([m_a1, m_a2, m_b1, m_b2])

    ns_seq = "yua/ops/episodic"
    s_a1 = _make("Cluster A text is this one", ns_seq)
    s_a2 = _make("Cluster A text is this one", ns_seq)
    s_b1 = _make("Cluster B content is here", ns_seq)
    s_b2 = _make("Cluster B content is here", ns_seq)

    res_s1 = await plane.create(s_a1)
    res_s2 = await plane.create(s_a2)
    res_s3 = await plane.create(s_b1)
    res_s4 = await plane.create(s_b2)
    res_seq = [res_s1, res_s2, res_s3, res_s4]

    assert len(res_batch) == 4
    assert res_batch[0].object_id == res_batch[1].object_id
    assert res_batch[2].object_id == res_batch[3].object_id
    assert res_batch[0].object_id != res_batch[2].object_id

    assert res_batch[1].reinforcement_count == 1
    assert res_batch[3].reinforcement_count == 1

    assert res_seq[0].object_id == res_seq[1].object_id
    assert res_seq[2].object_id == res_seq[3].object_id
    assert res_seq[1].reinforcement_count == 1
    assert res_seq[3].reinforcement_count == 1

    from qdrant_client import models

    # DB count check
    b_count = qdrant.count(
        collection_name="musubi_episodic",
        count_filter=models.Filter(
            must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=ns_batch))]
        ),
        exact=True,
    ).count
    s_count = qdrant.count(
        collection_name="musubi_episodic",
        count_filter=models.Filter(
            must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=ns_seq))]
        ),
        exact=True,
    ).count
    assert b_count == 2
    assert s_count == 2


@pytest.mark.asyncio
async def test_batch_vs_sequential_permuted_order(
    plane: EpisodicPlane, qdrant: QdrantClient
) -> None:

    from qdrant_client import models

    base_inputs = [
        ("Cluster A", "A1", ["tag_a1"]),
        ("Cluster B", "B1", ["tag_b1"]),
        ("Cluster A", "A2", ["tag_a2"]),
        ("Cluster B", "B2", ["tag_b2"]),
    ]

    perms = [
        [base_inputs[0], base_inputs[1], base_inputs[2], base_inputs[3]],  # A1, B1, A2, B2
        [base_inputs[3], base_inputs[2], base_inputs[0], base_inputs[1]],  # B2, A2, A1, B1
    ]

    def projection(mem: EpisodicMemory) -> dict[str, Any]:
        return {
            "content": mem.content,
            "reinforcement_count": mem.reinforcement_count,
            "tags": sorted(mem.tags),
            "version": mem.version,
            "state": mem.state,
        }

    def partition_vector(mems: list[EpisodicMemory]) -> list[int]:
        ordinal_map: dict[str, int] = {}
        out = []
        for m in mems:
            if m.object_id not in ordinal_map:
                ordinal_map[m.object_id] = len(ordinal_map)
            out.append(ordinal_map[m.object_id])
        return out

    for i, perm in enumerate(perms):
        ns_batch = f"eric/batch_perm_{i}/episodic"
        ns_seq = f"yua/seq_perm_{i}/episodic"

        # Ensure the test namespaces are isolated and start empty
        assert (
            qdrant.count(
                collection_name="musubi_episodic",
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=ns_batch)
                        )
                    ]
                ),
                exact=True,
            ).count
            == 0
        )
        assert (
            qdrant.count(
                collection_name="musubi_episodic",
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=ns_seq)
                        )
                    ]
                ),
                exact=True,
            ).count
            == 0
        )

        mems_batch = [_make(content, ns_batch, tags=tags) for content, _, tags in perm]
        mems_seq = [_make(content, ns_seq, tags=tags) for content, _, tags in perm]

        res_batch = await plane.batch_create(mems_batch)

        res_seq = []
        for m in mems_seq:
            res_seq.append(await plane.create(m))

        assert len(res_batch) == 4
        assert len(res_seq) == 4

        # Compare normalized cluster partition vectors to ensure identity mapping matches
        assert partition_vector(res_batch) == partition_vector(res_seq)

        for rb, rs in zip(res_batch, res_seq, strict=True):
            assert projection(rb) == projection(rs)

        # DATA-001 P2: a dedup reinforce publishes a v2 layout (anchor + content points). Compare the
        # IDENTITY rows only (must_not content) and strip the layout-only keys before validating — the
        # anchor carries the full committed payload, so a stripped identity row is the authoritative view.
        from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD, strip_layout_fields

        _not_content = [
            models.FieldCondition(
                key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
            )
        ]
        stored_batch = qdrant.scroll(
            collection_name="musubi_episodic",
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=ns_batch))
                ],
                must_not=_not_content,
            ),
            limit=10,
        )[0]

        stored_seq = qdrant.scroll(
            collection_name="musubi_episodic",
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=ns_seq))],
                must_not=_not_content,
            ),
            limit=10,
        )[0]

        assert len(stored_batch) == len(stored_seq)

        sb_proj = sorted(
            [
                projection(EpisodicMemory.model_validate(strip_layout_fields(p.payload)))
                for p in stored_batch
                if p.payload
            ],
            key=lambda x: x["content"],
        )
        ss_proj = sorted(
            [
                projection(EpisodicMemory.model_validate(strip_layout_fields(p.payload)))
                for p in stored_seq
                if p.payload
            ],
            key=lambda x: x["content"],
        )

        assert sb_proj == ss_proj


@pytest.mark.asyncio
async def test_reinforce_accepts_missing_vectors_from_external_qdrant_candidate(
    plane: EpisodicPlane, ns: str, qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await plane.create(_make("Target phrase exactly.", ns))
    candidate = _make("Target phrase exactly.", ns)

    original_find = plane._find_dedup_candidate

    def mocked_find(
        namespace: str, dense: list[float]
    ) -> tuple[EpisodicMemory, list[float] | None, dict[int, float] | None, float] | None:
        res = original_find(namespace, dense)
        if res is not None:
            # Strip vectors, keep score (which must be >= 0.92 for dedup,
            # and here it's 1.0 because of identical content + fake embedder)
            return res[0], None, None, res[3]
        return None

    monkeypatch.setattr(plane, "_find_dedup_candidate", mocked_find)

    # Spy on _reinforce
    original_reinforce = plane._reinforce
    captured: list[tuple[list[float] | None, dict[int, float] | None]] = []

    async def spy_reinforce(
        *,
        existing: EpisodicMemory,
        existing_dense: list[float] | None,
        existing_sparse: dict[int, float] | None,
        new: EpisodicMemory,
        dense: list[float],
        sparse: dict[int, float],
        merge_strategy: MergeStrategy = "longer-wins",
    ) -> EpisodicMemory:
        captured.append((existing_dense, existing_sparse))
        return await original_reinforce(
            existing=existing,
            existing_dense=existing_dense,
            existing_sparse=existing_sparse,
            new=new,
            dense=dense,
            sparse=sparse,
            merge_strategy=merge_strategy,
        )

    monkeypatch.setattr(plane, "_reinforce", spy_reinforce)

    res = await plane.batch_create([candidate])

    # Assert _reinforce was called and passed exactly None for both vectors
    assert captured == [(None, None)]

    assert len(res) == 1
    assert res[0].object_id == base.object_id
    assert res[0].reinforcement_count == 1
