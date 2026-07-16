"""Test contract for slice-plane-curated.

Runs against an in-memory Qdrant (`qdrant_client.QdrantClient(":memory:")`)
and the deterministic :class:`FakeEmbedder`. These are unit tests — no
network, no GPU.

Test Contract bullets covered (from [[04-data-model/curated-knowledge]]):

Per the Method-ownership rule (see
[[00-index/agent-guardrails#Method-ownership-rule]]), bullets that describe
**vault filesystem behaviour, file-watcher events, frontmatter parsing,
write-log echo detection, promotion lineage, large-file chunking, audit
logging, or operator-scope authorization** belong to downstream slices —
their code lives in `src/musubi/vault_sync/`,
`src/musubi/lifecycle/`, `src/musubi/planes/artifact/`, or `src/musubi/auth/`,
none of which is in this slice's `owns_paths`. Those bullets land here as
``@pytest.mark.skip`` with the named follow-up slice and a one-line reason.

Bullets implemented here are the ones whose code path lives in
`src/musubi/planes/curated/`:

- 1  read-from-qdrant returns the indexed fields.
- 5  identical-content save is a no-op (plane-level idempotency by
     ``(namespace, vault_path, body_hash)``).
- 16 bitemporal ``valid_until`` excludes from the default query.
- 17 supersession chain reads return the latest.
- 19 namespace isolation on the read path.

Property tests (21, 22) and integration tests (23, 24) are declared
out-of-scope in the slice's ``## Work log`` per the Closure Rule's third
state.
"""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.transitions import TransitionResult
from musubi.planes.curated import CuratedPlane
from musubi.store import bootstrap
from musubi.types.common import Err, Ok
from musubi.types.curated import CuratedKnowledge
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
def plane(qdrant: QdrantClient, coordinator: LifecycleTransitionCoordinator) -> CuratedPlane:
    # DATA-001 P2: a same-id body/frontmatter update publishes through the immutable-vector seam; wire
    # the coordinator + a curated-bound publisher + the dispatcher so the update path is not fail-closed.
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )
    from musubi.store.names import collection_for_plane

    coll = collection_for_plane("curated")
    publisher = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=coll)
    register_immutable_vector_dispatch(coordinator, {coll: publisher})
    return CuratedPlane(
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
    return "eric/claude-code/curated"


def _hash(body: str) -> str:
    """sha256 of a markdown body — what the watcher would compute."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _make(
    *,
    namespace: str,
    title: str = "CUDA 13 setup notes",
    content: str = "Install nvidia driver 575 and the CUDA 13 toolchain.",
    vault_path: str = "curated/eric/projects/musubi.md",
    topics: list[str] | None = None,
    body_hash: str | None = None,
    **extra: Any,
) -> CuratedKnowledge:
    """Build a :class:`CuratedKnowledge` with sane defaults."""
    return CuratedKnowledge(
        namespace=namespace,
        title=title,
        content=content,
        vault_path=vault_path,
        body_hash=body_hash or _hash(content),
        topics=topics or ["projects/musubi"],
        **extra,
    )


# ---------------------------------------------------------------------------
# Bullet 1 — read-from-qdrant returns the indexed fields
# ---------------------------------------------------------------------------


async def test_read_from_qdrant_returns_indexed_fields(plane: CuratedPlane, ns: str) -> None:
    saved = await plane.create(
        _make(
            namespace=ns,
            title="CUDA 13 setup notes",
            topics=["infrastructure/gpu", "projects/musubi"],
            tags=["cuda", "ubuntu-noble"],
        )
    )
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None
    # Spot-check every field the curated payload index in store/specs.py
    # expects to be readable straight off the Qdrant payload.
    assert fetched.object_id == saved.object_id
    assert fetched.namespace == ns
    assert fetched.title == "CUDA 13 setup notes"
    assert fetched.vault_path == "curated/eric/projects/musubi.md"
    assert fetched.body_hash == saved.body_hash
    assert set(fetched.topics) == {"infrastructure/gpu", "projects/musubi"}
    assert set(fetched.tags) == {"cuda", "ubuntu-noble"}
    assert fetched.state == "matured"
    assert fetched.musubi_managed is True


# ---------------------------------------------------------------------------
# Bullet 5 — identical content save is a no-op
# ---------------------------------------------------------------------------


async def test_identical_content_save_no_index_write(
    plane: CuratedPlane, ns: str, qdrant: QdrantClient
) -> None:
    """Idempotency: saving the same (vault_path, body_hash) twice leaves
    Qdrant untouched after the first write — no superseded chain, no version
    bump."""
    first = await plane.create(_make(namespace=ns))
    second = await plane.create(_make(namespace=ns))
    # Same id back; version unchanged; still exactly one point in Qdrant.
    assert second.object_id == first.object_id
    assert second.version == first.version
    assert second.body_hash == first.body_hash
    count = qdrant.count(collection_name="musubi_curated", exact=True).count
    assert count == 1


# ---------------------------------------------------------------------------
# Bullet 16 — bitemporal valid_until excludes from default query
# ---------------------------------------------------------------------------


async def test_bitemporal_valid_until_excludes_from_default_query(
    plane: CuratedPlane, ns: str
) -> None:
    """A fact whose ``valid_until`` has already passed should not appear in
    the default query (callers who want history pass ``valid_at=...``)."""
    now = datetime.now(UTC)
    expired = await plane.create(
        _make(
            namespace=ns,
            title="GPU-driver pin: nvidia 470",
            content="Pinning to nvidia driver 470 for legacy CUDA workloads.",
            vault_path="curated/eric/infra/gpu-driver-pin.md",
            valid_from=now - timedelta(days=30),
            valid_until=now - timedelta(days=1),
        )
    )
    current = await plane.create(
        _make(
            namespace=ns,
            title="GPU-driver pin: nvidia 575",
            content="Pinning to nvidia driver 575 for CUDA 13 workloads.",
            vault_path="curated/eric/infra/gpu-driver-current.md",
            valid_from=now - timedelta(days=1),
        )
    )
    results = await plane.query(namespace=ns, query="nvidia driver", limit=10)
    ids = {r.object_id for r in results}
    assert expired.object_id not in ids
    assert current.object_id in ids
    # And explicit valid_at in the past brings the expired row back.
    historic = await plane.query(
        namespace=ns,
        query="nvidia driver",
        limit=10,
        valid_at=now - timedelta(days=15),
    )
    historic_ids = {r.object_id for r in historic}
    assert expired.object_id in historic_ids


# ---------------------------------------------------------------------------
# Bullet 17 — supersession chain read returns the latest
# ---------------------------------------------------------------------------


async def test_supersession_chain_read_returns_latest(plane: CuratedPlane, ns: str) -> None:
    """Save A, then save B at the same ``vault_path`` with different
    ``body_hash``; reads return B and A is marked superseded."""
    a = await plane.create(_make(namespace=ns, content="version A"))
    b = await plane.create(_make(namespace=ns, content="version B"))
    # Different ids — the second save did not dedup.
    assert b.object_id != a.object_id
    # B is matured, A is superseded by B.
    assert b.state == "matured"
    assert b.supersedes == [a.object_id]
    fetched_a = await plane.get(namespace=ns, object_id=a.object_id)
    assert fetched_a is not None
    assert fetched_a.state == "superseded"
    assert fetched_a.superseded_by == b.object_id
    # The default query returns only B.
    results = await plane.query(namespace=ns, query="version", limit=10)
    ids = {r.object_id for r in results}
    assert b.object_id in ids
    assert a.object_id not in ids


# ---------------------------------------------------------------------------
# Bullet 19 — namespace isolation on the read path
# ---------------------------------------------------------------------------


async def test_isolation_read_enforcement(plane: CuratedPlane) -> None:
    a_ns = "eric/claude-code/curated"
    b_ns = "yua/livekit/curated"
    a = await plane.create(
        _make(namespace=a_ns, vault_path="curated/eric/a.md", content="only-in-a")
    )
    b = await plane.create(
        _make(namespace=b_ns, vault_path="curated/yua/b.md", content="only-in-b")
    )
    # Querying A's namespace never returns B.
    results_a = await plane.query(namespace=a_ns, query="only", limit=10)
    assert all(r.object_id != b.object_id for r in results_a)
    # Wrong-namespace get() returns None.
    assert await plane.get(namespace=a_ns, object_id=b.object_id) is None
    assert await plane.get(namespace=b_ns, object_id=a.object_id) is None


# ---------------------------------------------------------------------------
# Bullets deferred to downstream slices (not silent — see Closure Rule)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: include=body filesystem read lives "
    "in src/musubi/vault_sync/, not in this slice's owns_paths."
)
def test_read_with_include_body_reads_from_vault_filesystem() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: filesystem watcher + debounce live "
    "in src/musubi/vault_sync/, not in this slice's owns_paths."
)
def test_human_edit_triggers_reindex_after_debounce() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: reindex pipeline that recomputes "
    "body_hash and bumps version on file change lives in src/musubi/vault_sync/."
)
def test_reindex_updates_body_hash_and_version() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: file-move watcher event handler "
    "lives in src/musubi/vault_sync/, not in this slice's owns_paths."
)
def test_file_move_updates_vault_path_in_qdrant() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: the file-delete watcher event handler "
    "(soft-delete to vault/_archive/ + state=archived) lives in "
    "src/musubi/vault_sync/, not in this slice's owns_paths."
)
def test_file_delete_archives_and_marks_state() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: vault frontmatter parsing + "
    "object_id back-write lives in src/musubi/vault_sync/."
)
def test_frontmatter_missing_object_id_gets_generated_and_written_back() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: frontmatter validation + thoughts "
    "emission live in src/musubi/vault_sync/ (with a thoughts-plane dependency)."
)
def test_frontmatter_schema_invalid_file_is_not_indexed_and_emits_thought() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: vault writer authorization "
    "(musubi-managed flag) lives in src/musubi/vault_sync/writer.py."
)
def test_musubi_managed_true_file_accepts_system_write() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: vault writer authorization "
    "(musubi-managed flag) lives in src/musubi/vault_sync/writer.py."
)
def test_musubi_managed_false_file_rejects_system_write() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: write-log echo detection lives "
    "in src/musubi/vault_sync/, not in this slice's owns_paths."
)
def test_write_log_echo_detection_prevents_double_index() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: concept->curated promotion "
    "writes (file + index) live in src/musubi/lifecycle/, not in this slice's owns_paths."
)
def test_promotion_writes_file_and_index_atomically_enough() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: concept.promoted_to + "
    "curated.promoted_from linkage is set by the promotion worker in "
    "src/musubi/lifecycle/, not in this slice's owns_paths."
)
def test_promotion_links_concept_to_curated_via_promoted_to_and_promoted_from() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-plane-artifact: large-file chunking writes "
    "ArtifactChunk rows in src/musubi/planes/artifact/, not in this slice's owns_paths."
)
def test_large_file_chunks_body_as_artifact_and_references() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-engine: cross-namespace-read audit "
    "logging lives in src/musubi/lifecycle/, not in this slice's owns_paths."
)
def test_cross_namespace_reference_logged_in_audit() -> None:
    pass


@pytest.mark.skip(
    reason="deferred to slice-auth: operator-scope enforcement on hard delete "
    "lives in src/musubi/auth/, not in this slice's owns_paths."
)
def test_hard_delete_requires_operator_scope() -> None:
    pass


# ---------------------------------------------------------------------------
# Coverage tests — not Test Contract bullets, but they exercise the plane's
# transition + write-isolation paths so branch coverage clears the 90 % gate
# the guardrails require for src/musubi/planes/**.
# ---------------------------------------------------------------------------


async def test_transition_to_superseded_emits_lifecycle_event(plane: CuratedPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns))
    outcome = _final(
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="superseded",
            actor="test",
            reason="unit",
            coordinator=_coord(),
        )
    )
    updated = await plane.get(namespace=ns, object_id=saved.object_id)
    assert updated is not None
    event = outcome.event
    assert updated.state == "superseded"
    assert isinstance(event, LifecycleEvent)
    assert event.from_state == "matured"
    assert event.to_state == "superseded"
    assert event.object_type == "curated"


async def test_transition_to_archived_keeps_record_but_filters_default_reads(
    plane: CuratedPlane, ns: str
) -> None:
    saved = await plane.create(
        _make(namespace=ns, content="archive-me", vault_path="curated/eric/old.md")
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="archived",
        actor="t",
        reason="rm",
        coordinator=_coord(),
    )
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None and fetched.state == "archived"
    results = await plane.query(namespace=ns, query="archive-me", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_transition_illegal_raises(plane: CuratedPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns))
    # matured → demoted is not in the curated transition table.
    result = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="demoted",
        actor="t",
        reason="unit",
        coordinator=_coord(),
    )
    assert isinstance(result, Err)
    assert result.error.code == "illegal_transition"


async def test_transition_unknown_object_raises_lookup_error(plane: CuratedPlane, ns: str) -> None:
    missing = "0" * 27
    result = await plane.transition(
        namespace=ns,
        object_id=missing,
        to_state="superseded",
        actor="t",
        reason="unit",
        coordinator=_coord(),
    )
    assert isinstance(result, Err)
    assert result.error.code == "not_found"


async def test_isolation_write_enforcement(plane: CuratedPlane) -> None:
    a_ns = "eric/claude-code/curated"
    b_ns = "yua/livekit/curated"
    a = await plane.create(_make(namespace=a_ns, content="write-iso-a"))
    result = await plane.transition(
        namespace=b_ns,
        object_id=a.object_id,
        to_state="archived",
        actor="t",
        reason="unit",
        coordinator=_coord(),
    )
    assert isinstance(result, Err)
    assert result.error.code == "not_found"
    still = await plane.get(namespace=a_ns, object_id=a.object_id)
    assert still is not None and still.state == "matured"


async def test_get_returns_none_for_missing_id(plane: CuratedPlane, ns: str) -> None:
    missing = "0" * 27
    assert await plane.get(namespace=ns, object_id=missing) is None


async def test_create_auto_embeds_dense_and_sparse_vectors(
    plane: CuratedPlane, ns: str, qdrant: QdrantClient
) -> None:
    from qdrant_client import models as qmodels

    saved = await plane.create(_make(namespace=ns, content="embed me"))
    records, _ = qdrant.scroll(
        collection_name="musubi_curated",
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


async def test_create_same_object_id_new_body_updates_in_place(
    plane: CuratedPlane, ns: str
) -> None:
    """Regression for musubi#362.

    The supersession path was triggered any time `body_hash` differed
    from the existing row's hash — even when `memory.object_id ==
    existing.object_id` (the common case for vault reconcile: same file,
    edited body). It built `new_row(object_id=X, supersedes=[X])` which
    fails the MemoryObject invariant "object cannot appear in its own
    supersedes list."

    The fix: distinguish same-id-update from supersession. Same id +
    new body should produce an updated row at the SAME object_id, with
    `supersedes=[]` and an incremented version. Locks in the contract.
    """
    first = await plane.create(
        _make(namespace=ns, content="original body", vault_path="curated/eric/note.md")
    )

    # Reconciler-style: same file (same object_id, same vault_path),
    # edited body. Build a CuratedKnowledge with the existing object_id.
    edited = _make(
        namespace=ns,
        content="edited body",
        vault_path="curated/eric/note.md",
        object_id=first.object_id,
    )
    updated = await plane.create(edited)

    # Same logical row — id preserved.
    assert updated.object_id == first.object_id
    # Version bumped (update increments).
    assert updated.version == first.version + 1
    # New content reached storage.
    assert updated.content == "edited body"
    assert updated.body_hash != first.body_hash
    # Supersession invariants: NOT a supersession (so the dangerous
    # validator wouldn't have a chance to fail anyway).
    assert updated.supersedes == []
    assert updated.superseded_by is None

    # And it's actually fetchable from storage (round-trip via get).
    fetched = await plane.get(namespace=ns, object_id=first.object_id)
    assert fetched is not None
    assert fetched.content == "edited body"


async def test_create_same_object_id_update_propagates_frontmatter_fields(
    plane: CuratedPlane, ns: str
) -> None:
    """Companion to musubi#362 — make sure the update path doesn't
    silently drop fields that aren't body/title.

    The earlier Copilot review on PR #363 pointed out that hand-copying
    a subset of fields (content/title/summary/topics/tags/importance)
    would leave bitemporal validity (`valid_from`, `valid_until`),
    `musubi_managed`, and other frontmatter-driven fields stale across
    a reconcile. Update path now starts from the FULL incoming memory
    and preserves only identity/creation/lineage from existing.
    """
    now = datetime.now(UTC)
    first = await plane.create(
        _make(
            namespace=ns,
            content="original body",
            vault_path="curated/eric/dated.md",
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=30),
            musubi_managed=True,
        )
    )

    edited = _make(
        namespace=ns,
        content="edited body",
        vault_path="curated/eric/dated.md",
        object_id=first.object_id,
        # Frontmatter edits the operator might make:
        valid_from=now - timedelta(days=15),  # narrowed
        valid_until=now + timedelta(days=60),  # extended
        musubi_managed=False,  # operator un-flagged
        tags=["edited", "newtag"],
        importance=9,
    )
    updated = await plane.create(edited)

    # All edited fields reached storage.
    assert updated.valid_from is not None
    assert updated.valid_until is not None
    assert (now - timedelta(days=16)) <= updated.valid_from <= (now - timedelta(days=14))
    assert (now + timedelta(days=59)) <= updated.valid_until <= (now + timedelta(days=61))
    assert updated.musubi_managed is False
    assert set(updated.tags) == {"edited", "newtag"}
    assert updated.importance == 9
    # And identity/creation invariants preserved.
    assert updated.object_id == first.object_id
    assert updated.created_at == first.created_at


async def test_same_id_update_inherits_state_lineage_access_from_fresh(
    plane: CuratedPlane, ns: str
) -> None:
    """DATA-001 P2 (Yua): a same-id body/frontmatter update carries ONLY author-managed frontmatter.
    Lifecycle ``state`` (transitions own it), transition-owned lineage, and lease-owned access are
    INHERITED from the fresh committed row — a concurrent change to them survives, and the incoming
    memory can never set them — while the intended frontmatter lands and version bumps exactly once."""
    from qdrant_client import models as qmodels

    from musubi.types.common import generate_ksuid

    lineage_superseded = str(generate_ksuid())
    lineage_promoted = str(generate_ksuid())
    promoted_at_iso = datetime.now(UTC).isoformat()
    first = await plane.create(
        _make(namespace=ns, title="T1", content="body one", vault_path="curated/eric/inh.md")
    )
    # a concurrent transition-owned STATE + lineage + lease-owned-access change lands on the identity
    # row (a real state transition, not just lineage — so we prove the EXACT state survives).
    plane._client.set_payload(
        collection_name=plane._collection,
        payload={
            "state": "superseded",
            "superseded_by": lineage_superseded,
            "promoted_from": lineage_promoted,
            "promoted_at": promoted_at_iso,
            "access_count": 7,
        },
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=str(first.object_id))
                ),
                qmodels.FieldCondition(key="namespace", match=qmodels.MatchValue(value=ns)),
            ]
        ),
        wait=True,
    )
    # the incoming memory ALSO tries to set state=archived — the allowlist must ignore it.
    updated = await plane.create(
        _make(
            namespace=ns,
            title="T2-edited",
            content="a longer edited body two",
            vault_path="curated/eric/inh.md",
            object_id=first.object_id,
            state="archived",
        )
    )
    assert updated.object_id == first.object_id
    assert updated.title == "T2-edited" and updated.content == "a longer edited body two"  # landed
    assert updated.version == first.version + 1  # bumped exactly once
    # the CONCURRENT state transition survives, and the incoming state=archived is ignored (allowlist).
    assert updated.state == "superseded", (
        "a concurrent transition-owned state change must survive; incoming state=archived must be ignored"
    )
    assert str(updated.superseded_by) == lineage_superseded, "transition-owned lineage must survive"
    assert str(updated.promoted_from) == lineage_promoted
    assert updated.promoted_at is not None, "promoted_at must survive alongside promoted_from"
    assert updated.access_count == 7, "lease-owned access must survive"


async def test_patch_metadata_preserves_concurrent_state_access_bumps_version_once(
    plane: CuratedPlane, ns: str
) -> None:
    """DATA-001 P2 (Yua): the metadata-only PATCH routes through the attributable Phase-1 mutation lease
    (owned_update) — it targets the identity row (v1 here), bumps version once, and a concurrent
    transition-owned state + lease-owned access change SURVIVES while the intended metadata lands."""
    from qdrant_client import models as qmodels

    from musubi.types.common import generate_ksuid

    first = await plane.create(
        _make(namespace=ns, title="T", content="body", vault_path="curated/eric/patch.md")
    )
    # a concurrent transition + access bump lands on the (v1) identity row.
    superseded_by = str(generate_ksuid())
    plane._client.set_payload(
        collection_name=plane._collection,
        payload={"state": "superseded", "superseded_by": superseded_by, "access_count": 5},
        points=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="object_id", match=qmodels.MatchValue(value=str(first.object_id))
                ),
                qmodels.FieldCondition(key="namespace", match=qmodels.MatchValue(value=ns)),
            ]
        ),
        wait=True,
    )
    updated = await plane.patch_metadata(
        namespace=ns, object_id=first.object_id, changes={"tags": ["x", "y"], "importance": 9}
    )
    assert set(updated.tags) == {"x", "y"} and updated.importance == 9  # metadata landed
    assert updated.version == first.version + 1  # bumped exactly once
    assert updated.state == "superseded", "concurrent transition-owned state must survive the PATCH"
    assert str(updated.superseded_by) == superseded_by
    assert updated.access_count == 5, "lease-owned access must survive the PATCH"


async def test_patch_curated_router_refuses_dangling_pointer_without_mutation(
    plane: CuratedPlane, ns: str
) -> None:
    """DATA-001 P2 (Yua): the ACTUAL patch_curated router must REFUSE (409 CONFLICT) a PATCH against a
    v2 object whose committed content point is gone, and must NOT mutate the identity row."""
    from musubi.api.errors import APIError
    from musubi.api.routers.writes_curated import PatchCuratedRequest, patch_curated
    from musubi.store.immutable_vectors import read_anchor

    # first create is v1; a same-id body update promotes it to v2 (anchor + content), then dangle it.
    first = await plane.create(
        _make(namespace=ns, title="T", content="c1", vault_path="curated/eric/dg.md")
    )
    await plane.create(
        _make(
            namespace=ns,
            title="T",
            content="c2-is-longer",
            vault_path="curated/eric/dg.md",
            object_id=first.object_id,
        )
    )
    anchor = read_anchor(
        plane._client, plane._collection, namespace=ns, object_id=str(first.object_id)
    )
    assert anchor is not None and anchor.live_point is not None
    plane._client.delete(collection_name=plane._collection, points_selector=[anchor.live_point])

    before = await plane.raw_payload(namespace=ns, object_id=str(first.object_id))
    with pytest.raises(APIError) as exc:
        await patch_curated(
            object_id=str(first.object_id),
            namespace=ns,
            body=PatchCuratedRequest(tags=["x"], importance=3),
            qdrant=plane._client,
            plane=plane,
        )
    assert exc.value.status_code == 409, (
        "a dangling-pointer PATCH must fail closed with 409 CONFLICT"
    )
    after = await plane.raw_payload(namespace=ns, object_id=str(first.object_id))
    assert after == before, "a refused PATCH must not mutate the identity row"


async def test_query_excludes_archived_by_default(plane: CuratedPlane, ns: str) -> None:
    saved = await plane.create(
        _make(namespace=ns, content="hidden-me", vault_path="curated/eric/hidden.md")
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="archived",
        actor="t",
        reason="rm",
        coordinator=_coord(),
    )
    results = await plane.query(namespace=ns, query="hidden-me", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_query_respects_limit(plane: CuratedPlane, ns: str) -> None:
    for i in range(5):
        await plane.create(
            _make(
                namespace=ns,
                content=f"limit-fixture-{i}",
                vault_path=f"curated/eric/limit-{i}.md",
            )
        )
    results = await plane.query(namespace=ns, query="limit-fixture", limit=3)
    assert len(results) <= 3


@pytest.mark.anyio
async def test_scan_vault_rows_paginates_and_validates(
    plane: CuratedPlane, ns: str, qdrant: QdrantClient
) -> None:

    # Create separate objects so each page contains distinct validated rows.
    await plane.create(_make(namespace=ns, title="row1", vault_path="path1.md"))
    await plane.create(_make(namespace=ns, title="row2", vault_path="path2.md"))
    await plane.create(_make(namespace=ns, title="row3", vault_path="path3.md"))

    # Patch the synchronous scroll seam to return two deterministic pages.
    original_scroll = plane._client.scroll

    all_records, _ = original_scroll(
        collection_name=plane._collection, limit=10, with_payload=True, with_vectors=False
    )

    def mock_scroll(*args: Any, offset: Any = None, **kwargs: Any) -> tuple[list[Any], int | None]:
        # Only paginate the top-level inventory scroll (limit=1000). DATA-001 P2: scan_vault_rows now
        # RESOLVES each identity through its anchor, whose internal scrolls must reach the real client —
        # delegate anything that is not the inventory page, or we would starve the resolver.
        if kwargs.get("limit") != 1000:
            return original_scroll(*args, offset=offset, **kwargs)
        if offset is None:
            return all_records[:2], 2  # return first 2, next offset is 2
        else:
            return all_records[2:], None  # return remaining, offset None

    with patch.object(plane._client, "scroll", side_effect=mock_scroll):
        rows = await plane.scan_vault_rows()

    assert len(rows) == 3
    paths = [r.vault_path for r in rows]
    assert "path1.md" in paths
    assert "path2.md" in paths
    assert "path3.md" in paths


@pytest.mark.anyio
async def test_scan_vault_rows_surfaces_validation_failure(
    plane: CuratedPlane, ns: str, qdrant: QdrantClient
) -> None:
    from qdrant_client.models import PointStruct

    from musubi.store.specs import DENSE_VECTOR_NAME

    # Seed a v1-shape identity row (has object_id + namespace so it RESOLVES as a legacy self-pointer)
    # but missing required schema fields, so the post-resolve model_validate raises. DATA-001 P2: the
    # scan resolves through the anchor first, then validates — the fail-loud surface for a corrupt row
    # is still the pydantic validation error (Yua: validation failures must propagate, never be skipped).
    qdrant.upsert(
        collection_name=plane._collection,
        points=[
            PointStruct(
                id="00000000-0000-0000-0000-000000000000",
                vector={DENSE_VECTOR_NAME: [0.0] * 1024},
                payload={
                    "object_id": "bad-oid",
                    "namespace": ns,
                    "vault_path": "bad.md",
                    "invalid_schema": "missing_required_fields",
                },
            )
        ],
    )

    with pytest.raises(Exception) as excinfo:
        await plane.scan_vault_rows()

    # pydantic validation error
    assert "validation error" in str(excinfo.value).lower()


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{}, None])
async def test_scan_vault_rows_rejects_empty_or_missing_payload(
    plane: CuratedPlane, payload: dict[str, object] | None
) -> None:
    point = MagicMock(payload=payload)
    with (
        patch.object(plane._client, "scroll", return_value=([point], None)),
        pytest.raises((ValueError, ValidationError)),
    ):
        await plane.scan_vault_rows()
