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
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.curated import CuratedPlane
from musubi.store import bootstrap
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


@pytest.fixture
def plane(qdrant: QdrantClient) -> CuratedPlane:
    return CuratedPlane(client=qdrant, embedder=FakeEmbedder())


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
    updated, event = await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="superseded",
        actor="test",
        reason="unit",
    )
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
    )
    fetched = await plane.get(namespace=ns, object_id=saved.object_id)
    assert fetched is not None and fetched.state == "archived"
    results = await plane.query(namespace=ns, query="archive-me", limit=10)
    assert all(r.object_id != saved.object_id for r in results)


async def test_transition_illegal_raises(plane: CuratedPlane, ns: str) -> None:
    saved = await plane.create(_make(namespace=ns))
    # matured → demoted is not in the curated transition table.
    with pytest.raises(ValueError):
        await plane.transition(
            namespace=ns,
            object_id=saved.object_id,
            to_state="demoted",
            actor="t",
            reason="unit",
        )


async def test_transition_unknown_object_raises_lookup_error(plane: CuratedPlane, ns: str) -> None:
    missing = "0" * 27
    with pytest.raises(LookupError):
        await plane.transition(
            namespace=ns,
            object_id=missing,
            to_state="superseded",
            actor="t",
            reason="unit",
        )


async def test_isolation_write_enforcement(plane: CuratedPlane) -> None:
    a_ns = "eric/claude-code/curated"
    b_ns = "yua/livekit/curated"
    a = await plane.create(_make(namespace=a_ns, content="write-iso-a"))
    with pytest.raises(LookupError):
        await plane.transition(
            namespace=b_ns,
            object_id=a.object_id,
            to_state="archived",
            actor="t",
            reason="unit",
        )
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
