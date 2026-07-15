"""RET-011 / Issue #510 — exact deployment-namespace retrieval consistency.

Invariant (Yua ruling 2026-07-15, #510 supersedes #332 for a concrete target):

    A CONCRETE deployment namespace target (tenant/presence/plane) must return ONLY that
    presence's rows — never a sibling presence in the same identity family. Cross-presence
    (identity-family) retrieval is authorized ONLY when the request explicitly resolves
    multiple concrete targets (wildcard expansion / explicit namespace_targets). Synthesis
    family federation is unchanged and out of scope here.

The leak: fast/deep/blended funnel into `hybrid._build_filter`, which scoped to
`identity_family = family_of(namespace)` — every presence of one identity visible from any.
`recent` already filters exact `namespace` and is the reference-correct behavior. There is a
SECOND leak: `fast._cache_key` keyed on `family_of(namespace)`, so two presences shared a
fast-response cache entry.

These are RED on current main: seed two presences of ONE family with IDENTICAL content (so
the embedding vector is identical and cannot discriminate — only the namespace filter can),
target one presence, and the other leaks in. `recent` and the wildcard/multi-target
non-regression are green before and after.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from qdrant_client import QdrantClient

from musubi.embedding.fake import FakeEmbedder
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.fast import FastResponseCache, run_fast_retrieve
from musubi.retrieve.orchestration import NamespaceTarget, RetrievalQuery
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.episodic import EpisodicMemory

pytestmark = pytest.mark.asyncio

# Two presences of ONE identity family ("eric"). Same family → the OLD identity_family filter
# treated them as interchangeable. Same content below → identical FakeEmbedder vectors, so the
# namespace filter is the ONLY thing that can keep them apart.
_PRES_A = "eric/presalpha/episodic"
_PRES_B = "eric/presbravo/episodic"
_CONTENT = "identical shared marker content that both presences store verbatim"


class _FakeReranker:
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [float(1.0 - i * 0.01) for i in range(len(texts))]


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def reranker() -> _FakeReranker:
    return _FakeReranker()


async def _seed(client: QdrantClient, emb: FakeEmbedder, ns: str) -> str:
    row = await EpisodicPlane(client=client, embedder=emb).create(
        EpisodicMemory(namespace=ns, content=_CONTENT, state="matured")
    )
    return row.object_id


async def _seed_both(client: QdrantClient, emb: FakeEmbedder) -> tuple[str, str]:
    return await _seed(client, emb, _PRES_A), await _seed(client, emb, _PRES_B)


async def _retrieve(
    client: QdrantClient,
    emb: FakeEmbedder,
    rer: Any,
    *,
    targets: list[str],
    mode: str,
) -> list[Any]:
    q = RetrievalQuery(
        namespace=targets[0],
        query_text="identical shared marker",
        mode=mode,  # type: ignore[arg-type]
        limit=10,
        planes=["episodic"],
        state_filter=["provisional", "matured", "promoted"],
        namespace_targets=[NamespaceTarget(namespace=ns, plane="episodic") for ns in targets],
    )
    res = await run_orchestration_retrieve(client, emb, rer, query=q)
    assert res.is_ok(), res
    return list(res.unwrap().results)


# ═══ concrete target must not leak the sibling presence (fast/deep/blended) ════
@pytest.mark.parametrize("mode", ["fast", "deep", "blended"])
async def test_concrete_target_does_not_leak_sibling_presence(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker, mode: str
) -> None:
    a_id, b_id = await _seed_both(qdrant, embedder)
    rows = await _retrieve(qdrant, embedder, reranker, targets=[_PRES_A], mode=mode)

    namespaces = {r.namespace for r in rows}
    ids = {r.object_id for r in rows}
    assert a_id in ids, f"{mode}: the targeted presence's own row must be returned"
    assert namespaces == {_PRES_A}, (
        f"{mode}: concrete target {_PRES_A} leaked sibling presence(s) {namespaces - {_PRES_A}}"
    )
    assert b_id not in ids, f"{mode}: sibling presence {_PRES_B} must never be delivered"


# ═══ recent is already presence-exact — the reference the others must match ════
async def test_recent_concrete_target_is_presence_exact(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    _a_id, b_id = await _seed_both(qdrant, embedder)
    rows = await _retrieve(qdrant, embedder, reranker, targets=[_PRES_A], mode="recent")
    assert {r.namespace for r in rows} == {_PRES_A}
    assert b_id not in {r.object_id for r in rows}


# ═══ the fast-response cache must not serve a sibling presence ═════════════════
async def test_fast_cache_does_not_serve_sibling_presence(
    qdrant: QdrantClient, embedder: FakeEmbedder
) -> None:
    """Second leak: a shared FastResponseCache keyed on family_of() serves presence A's rows to a
    presence B query. Drive run_fast_retrieve twice with ONE cache; B must get only B's row."""
    a_id, b_id = await _seed_both(qdrant, embedder)
    cache = FastResponseCache()
    collection = collection_for_plane("episodic")

    res_a = await run_fast_retrieve(
        qdrant,
        embedder,
        namespace=_PRES_A,
        query="identical shared marker",
        collection=collection,
        limit=10,
        state_filter=["provisional", "matured", "promoted"],
        response_cache=cache,
        now=1000.0,
    )
    res_b = await run_fast_retrieve(
        qdrant,
        embedder,
        namespace=_PRES_B,
        query="identical shared marker",
        collection=collection,
        limit=10,
        state_filter=["provisional", "matured", "promoted"],
        response_cache=cache,
        now=1000.0,
    )
    assert res_a.is_ok() and res_b.is_ok(), (res_a, res_b)
    result_b = res_b.unwrap()
    assert result_b.cache_hit is False, (
        "presence B must MISS the cache — a family-keyed hit is the leak"
    )
    b_ids = {h.object_id for h in result_b.results}
    b_namespaces = {h.payload.get("namespace") for h in result_b.results}
    assert b_id in b_ids, "presence B's own row must be returned to a B query"
    assert a_id not in b_ids, "fast cache leaked presence A's row to a presence B query"
    assert b_namespaces == {_PRES_B}, f"fast cache crossed presences: {b_namespaces}"


# ═══ NON-regression: explicit multi-target (wildcard-expanded) still unions ════
@pytest.mark.parametrize("mode", ["fast", "deep", "blended", "recent"])
async def test_explicit_multi_target_still_returns_all_presences(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker, mode: str
) -> None:
    """Family coverage is authorized via EXPLICIT resolution to multiple concrete targets (what
    wildcard expansion produces). Exact per-leg filtering must still union to both presences.

    DISTINCT content per presence — blended intentionally content-hash-dedups identical rows
    across presences, which is orthogonal to namespace scoping; distinct memories keep the
    union observable for every mode."""
    ep = EpisodicPlane(client=qdrant, embedder=embedder)
    a = await ep.create(
        EpisodicMemory(
            namespace=_PRES_A, content="shared marker alpha distinct body", state="matured"
        )
    )
    b = await ep.create(
        EpisodicMemory(
            namespace=_PRES_B, content="shared marker bravo distinct body", state="matured"
        )
    )
    rows = await _retrieve(qdrant, embedder, reranker, targets=[_PRES_A, _PRES_B], mode=mode)
    ids = {r.object_id for r in rows}
    assert {a.object_id, b.object_id} <= ids, (
        f"{mode}: explicit multi-target must union both presences, got {ids}"
    )
    assert {r.namespace for r in rows} == {_PRES_A, _PRES_B}
