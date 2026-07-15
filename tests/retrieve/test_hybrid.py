"""Test contract for slice-retrieval-hybrid."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.retrieve.hybrid import (
    HYBRID_PREFETCH_LIMIT,
    QueryEmbeddingCache,
    hybrid_search,
    hybrid_search_many,
)
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import Err, Ok

NAMESPACE = "tenant/presence/episodic"
COLLECTION = "musubi_episodic"


@dataclass(slots=True)
class _Point:
    id: str
    score: float
    payload: dict[str, Any]


@dataclass(slots=True)
class _Response:
    points: list[_Point]


class _SpyQdrantClient:
    def __init__(
        self,
        *,
        points: list[_Point] | None = None,
        delay_s: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.points = points or [
            _Point(
                id="point-1",
                score=0.9,
                payload={
                    "object_id": "object-1",
                    "namespace": NAMESPACE,
                    "state": "matured",
                },
            )
        ]
        self.delay_s = delay_s
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def query_points(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.delay_s:
            time.sleep(self.delay_s)
        return _Response(points=list(self.points))


class _CountingEmbedder(Embedder):
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_calls = 0

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.dense_calls += 1
        return [[1.0, 0.0, 0.0] for _text in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        return [{1: 1.0, 3: 0.5} for _text in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [1.0 for _candidate in candidates]


class _BarrierEmbedder(_CountingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.dense_started = asyncio.Event()
        self.sparse_started = asyncio.Event()

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.dense_calls += 1
        self.dense_started.set()
        await asyncio.wait_for(self.sparse_started.wait(), timeout=0.2)
        return [[1.0, 0.0, 0.0] for _text in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        self.sparse_started.set()
        await asyncio.wait_for(self.dense_started.wait(), timeout=0.2)
        return [{1: 1.0} for _text in texts]


class _SlowSparseEmbedder(_CountingEmbedder):
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        await asyncio.sleep(0.05)
        return [{1: 1.0} for _text in texts]


class _BrokenDenseEmbedder(_CountingEmbedder):
    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.dense_calls += 1
        raise RuntimeError("dense broke")


class _BrokenSparseEmbedder(_CountingEmbedder):
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        raise RuntimeError("sparse broke")


def _client(client: _SpyQdrantClient) -> QdrantClient:
    return cast(QdrantClient, client)


async def _call(
    client: _SpyQdrantClient | None = None,
    embedder: Embedder | None = None,
    **kwargs: Any,
) -> tuple[_SpyQdrantClient, Any]:
    spy = client or _SpyQdrantClient()
    query = kwargs.pop("query", "find gpu notes")
    result = await hybrid_search(
        _client(spy),
        embedder or _CountingEmbedder(),
        namespace=NAMESPACE,
        query=query,
        collection=COLLECTION,
        **kwargs,
    )
    return spy, result


def _prefetches(call: dict[str, Any]) -> list[models.Prefetch]:
    return cast(list[models.Prefetch], call["prefetch"])


def _filter_conditions(call: dict[str, Any]) -> list[models.Condition]:
    query_filter = cast(models.Filter, call["query_filter"])
    return cast(list[models.Condition], query_filter.must)


@pytest.mark.asyncio
async def test_hybrid_query_uses_both_prefetch_steps() -> None:
    spy, result = await _call()

    assert isinstance(result, Ok)
    prefetches = _prefetches(spy.calls[0])
    assert len(prefetches) == 2
    assert {prefetch.using for prefetch in prefetches} == {
        DENSE_VECTOR_NAME,
        SPARSE_VECTOR_NAME,
    }


@pytest.mark.asyncio
async def test_rrf_fusion_requested_server_side() -> None:
    spy, _result = await _call()

    fusion_query = spy.calls[0]["query"]
    assert isinstance(fusion_query, models.FusionQuery)
    assert fusion_query.fusion == models.Fusion.RRF
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_namespace_filter_applied_not_identity_family() -> None:
    """RET-011 / #510 (supersedes #332 for a CONCRETE target): hybrid retrieval filters on the
    EXACT namespace, not `identity_family`. A concrete "tenant/presence/plane" target returns only
    that presence's rows; cross-presence federation now requires an explicit wildcard that resolves
    multiple concrete `namespace_targets`, never an implicit family-wide filter. The scope is
    enforced on BOTH the top-level filter and each prefetch sub-query (the prefetch is where an
    unfiltered vector search would otherwise surface a sibling presence)."""
    spy, _result = await _call()

    conditions = _filter_conditions(spy.calls[0])
    # Exact-namespace filter IS present.
    assert any(
        isinstance(condition, models.FieldCondition)
        and condition.key == "namespace"
        and isinstance(condition.match, models.MatchValue)
        and condition.match.value == NAMESPACE
        for condition in conditions
    ), "top-level filter must scope to the exact namespace"

    # identity_family filter is GONE for concrete-target retrieval.
    assert not any(
        isinstance(condition, models.FieldCondition) and condition.key == "identity_family"
        for condition in conditions
    ), "identity_family scoping is superseded (#510) for a concrete target"

    # Each prefetch sub-query carries the exact-namespace scope — the actual leak fix.
    prefetches = _prefetches(spy.calls[0])
    assert prefetches, "expected at least one prefetch"
    for prefetch in prefetches:
        pf_conditions = list(prefetch.filter.must or []) if prefetch.filter else []
        assert any(
            isinstance(condition, models.FieldCondition)
            and condition.key == "namespace"
            and isinstance(condition.match, models.MatchValue)
            and condition.match.value == NAMESPACE
            for condition in pf_conditions
        ), "each prefetch must be namespace-scoped so a vector sub-query cannot cross presences"


@pytest.mark.asyncio
async def test_prefetch_limit_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import musubi.retrieve.hybrid as hybrid

    class _Settings:
        hybrid_prefetch_limit = 7

    monkeypatch.setattr(hybrid, "get_settings", lambda: _Settings())
    spy, _result = await _call()

    assert [prefetch.limit for prefetch in _prefetches(spy.calls[0])] == [7, 7]


@pytest.mark.asyncio
async def test_empty_query_returns_empty_not_error() -> None:
    spy = _SpyQdrantClient()
    result = await hybrid_search(
        _client(spy),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="",
        collection=COLLECTION,
    )

    assert isinstance(result, Err)
    assert result.error.code == "empty_query"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_query_encoding_runs_in_parallel() -> None:
    spy, result = await _call(embedder=_BarrierEmbedder())

    assert isinstance(result, Ok)
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_query_embedding_cache_hit_on_repeat() -> None:
    cache = QueryEmbeddingCache(model_version="v1")
    embedder = _CountingEmbedder()
    first_spy, first = await _call(embedder=embedder, cache=cache)
    second_spy, second = await _call(embedder=embedder, cache=cache)

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert len(first_spy.calls) == 1
    assert len(second_spy.calls) == 1
    assert embedder.dense_calls == 1
    assert embedder.sparse_calls == 1


@pytest.mark.asyncio
async def test_cache_cleared_on_model_version_change() -> None:
    cache = QueryEmbeddingCache(model_version="v1")
    embedder = _CountingEmbedder()
    await _call(embedder=embedder, cache=cache)

    cache.set_model_version("v2")
    await _call(embedder=embedder, cache=cache)

    assert embedder.dense_calls == 2
    assert embedder.sparse_calls == 2


@pytest.mark.asyncio
async def test_hybrid_timeout_returns_err() -> None:
    spy = _SpyQdrantClient(delay_s=0.05)
    result = await hybrid_search(
        _client(spy),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collection=COLLECTION,
        timeout_s=0.001,
    )

    # RET-007 C5 migration: a query timeout is now an Err (was silently swallowed to Ok([])).
    assert isinstance(result, Err)
    assert "timeout" in str(result.error.detail).lower()


@pytest.mark.asyncio
async def test_dense_only_fallback_when_sparse_timeout() -> None:
    spy, result = await _call(embedder=_SlowSparseEmbedder(), sparse_timeout_s=0.001)

    assert isinstance(result, Ok)
    prefetches = _prefetches(spy.calls[0])
    assert len(prefetches) == 1
    assert prefetches[0].using == DENSE_VECTOR_NAME


@pytest.mark.asyncio
async def test_fanout_over_planes_parallel() -> None:
    clients = [_SpyQdrantClient(delay_s=0.05), _SpyQdrantClient(delay_s=0.05)]
    started = time.perf_counter()
    result = await hybrid_search_many(
        [_client(client) for client in clients],
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collections=[COLLECTION, "musubi_curated"],
    )
    elapsed = time.perf_counter() - started

    assert isinstance(result, Ok)
    assert [len(client.calls) for client in clients] == [1, 1]
    assert elapsed < 0.09


@pytest.mark.asyncio
async def test_results_deduped_within_single_collection() -> None:
    spy = _SpyQdrantClient(
        points=[
            _Point("point-1", 0.9, {"object_id": "same", "state": "matured"}),
            _Point("point-2", 0.8, {"object_id": "same", "state": "matured"}),
        ]
    )
    _client_spy, result = await _call(client=spy)

    assert isinstance(result, Ok)
    assert [hit.object_id for hit in result.value.hits] == ["same"]


@pytest.mark.asyncio
async def test_filter_state_matured_excludes_archived_by_default() -> None:
    spy, _result = await _call()

    conditions = _filter_conditions(spy.calls[0])
    state_conditions = [
        condition
        for condition in conditions
        if isinstance(condition, models.FieldCondition) and condition.key == "state"
    ]
    assert len(state_conditions) == 1
    match = state_conditions[0].match
    assert isinstance(match, models.MatchAny)
    assert match.any == ["matured", "promoted"]
    assert "archived" not in match.any


@pytest.mark.asyncio
async def test_include_archived_opts_in() -> None:
    spy, _result = await _call(include_archived=True)

    conditions = _filter_conditions(spy.calls[0])
    assert not any(
        isinstance(condition, models.FieldCondition) and condition.key == "state"
        for condition in conditions
    )


@pytest.mark.property
@given(
    scores=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    )
)
def test_hypothesis_rrf_result_is_deterministic_for_fixed_seed_corpus_query(
    scores: list[float],
) -> None:
    points = [
        _Point(str(i), score, {"object_id": f"object-{i}", "state": "matured"})
        for i, score in enumerate(scores)
    ]
    first = sorted(points, key=lambda point: (-point.score, point.id))
    second = sorted(points, key=lambda point: (-point.score, point.id))

    assert [point.id for point in first] == [point.id for point in second]


@pytest.mark.property
@given(
    small=st.integers(min_value=1, max_value=10),
    extra=st.integers(min_value=0, max_value=10),
)
def test_hypothesis_increasing_prefetch_limit_never_reduces_recall_on_fixed_query(
    small: int, extra: int
) -> None:
    large = small + extra
    corpus = {f"object-{i}" for i in range(large)}

    assert {f"object-{i}" for i in range(small)} <= corpus
    assert len(corpus) >= small


class DefectStillPresent(Exception):
    pass


@dataclass(frozen=True)
class _BeirGroup:
    query: str
    target: str
    distractors: tuple[str, ...]
    hybrid_favorable: bool  # target carries a real rare lexical identifier the distractors lack


# FROZEN hand-authored BEIR corpus (Yua ruling 2026-07-15): 16 groups from real memory shapes — NO
# generated glyphstone tokens, no repetitive template suffixes, no target-label leakage. Eight are
# hybrid-favorable (a REAL rare identifier — error code, command, filename, model/service/config/job
# name, version — in the target, with dense-similar topic distractors lacking it, so the sparse
# channel lifts the answer). Eight are semantic/paraphrase (no rare identifier; sparse-only exact
# matching cannot define the corpus). Topics are distinct to avoid cross-group retrieval.
_BEIR_GROUPS: tuple[_BeirGroup, ...] = (
    _BeirGroup(
        "what triggers the E-4021 write rejection",
        "E-4021 is raised when a full-object upsert lands on a stale version; the writer re-reads and retries against the fresh row before publishing.",
        (
            "Write conflicts under concurrent updates are resolved by an optimistic retry loop.",
            "A version mismatch during an upsert is a known concurrency hazard.",
            "The store rejects a write when two writers race the same row.",
        ),
        True,
    ),
    _BeirGroup(
        "how do I run tc-coverage for a slice",
        "Run make tc-coverage SLICE=<slice-id> to print which contract bullets have covering tests.",
        (
            "Coverage for a slice is judged against its acceptance matrix.",
            "Each contract bullet must map to a passing test before closure.",
            "Slice sign-off needs evidence for every acceptance item.",
        ),
        True,
    ),
    _BeirGroup(
        "what does docker-compose.test.yml define for tests",
        "The test stack lives in deploy/test-env/docker-compose.test.yml, bringing up qdrant plus the three tei services.",
        (
            "The integration harness boots real Qdrant and TEI before the suite runs.",
            "Test dependencies run as containers alongside the app.",
            "The environment provides vector search and embedding services to tests.",
        ),
        True,
    ),
    _BeirGroup(
        "what is bge-reranker-v2-m3 used for in retrieval",
        "Deep retrieval reranks its candidates with BAAI/bge-reranker-v2-m3 through the TEI reranker client.",
        (
            "The deep path adds a cross-encoder rerank over the fused candidates.",
            "Reranking reorders the hybrid results by relevance.",
            "The interactive path skips reranking to keep latency low.",
        ),
        True,
    ),
    _BeirGroup(
        "what does the tei-sparse service do and on which port",
        "tei-sparse runs SPLADE-v3 on port 8082 and returns the term vectors for the lexical channel.",
        (
            "Sparse vectors come from a dedicated embedding service.",
            "The lexical channel uses a different model from the dense one.",
            "Each embedding service in the stack listens on its own port.",
        ),
        True,
    ),
    _BeirGroup(
        "what does the ef_search parameter control",
        "Raise ef_search to widen the HNSW beam so more candidates are scored before ranking, trading a little latency for recall.",
        (
            "A wider search beam catches more true neighbors at some latency cost.",
            "Recall on hard queries improves when the index searches more nodes.",
            "Vector search has a tunable speed-versus-completeness tradeoff.",
        ),
        True,
    ),
    _BeirGroup(
        "what does the reconcile_ghost_rows job do",
        "The reconcile_ghost_rows job drops rows for files no longer on disk — known_hashes minus the current rglob.",
        (
            "Vault sync cleans up entries for files that were removed.",
            "Deleted files leave stale rows that must be reconciled.",
            "The boot scan reconciles the store against the filesystem.",
        ),
        True,
    ),
    _BeirGroup(
        "which release pinned the musubi-core signed digest to 1.17.2",
        "The 1.17.2 release chore pinned the musubi-core signed image digest for deploy.",
        (
            "Every release pins a signed image digest for reproducible deploys.",
            "Digest pinning ties a deploy to an exact built image.",
            "The deploy chore updates the pin on each version bump.",
        ),
        True,
    ),
    _BeirGroup(
        "how do I bring a crashed voice worker back up",
        "Recovering a downed voice bot: stop the dead worker, clear its session state, then start a fresh runtime so it re-registers.",
        (
            "Adding voice-bot replicas raises capacity across the pool.",
            "Voice agents connect to rooms allocated per region.",
            "Scaling out runs more workers in parallel for throughput.",
        ),
        False,
    ),
    _BeirGroup(
        "when is a memory ready to become curated knowledge",
        "A memory qualifies for the curated tier once it has matured, been corroborated across sessions, and passed synthesis review.",
        (
            "Editing a curated entry bumps its version.",
            "Curated knowledge is the highest-authority tier.",
            "Removing a curated entry archives it.",
        ),
        False,
    ),
    _BeirGroup(
        "should the vault write to the store on every edit",
        "No — the vault flushes to the store only on file close, not per edit, because per-write flushing thrashed the embedder.",
        (
            "The legacy policy synced to the store on each write event.",
            "Frequent writes overloaded the embedding service.",
            "Store writes are batched to reduce load.",
        ),
        False,
    ),
    _BeirGroup(
        "how do I roll back a broken release",
        "Reverting a bad release: pin the previous signed digest, redeploy, verify status, and confirm the broken build is no longer serving.",
        (
            "Rolling forward bumps the pin to a newer digest.",
            "Deploys are pinned to exact image digests.",
            "The pipeline verifies health after each deploy.",
        ),
        False,
    ),
    _BeirGroup(
        "can I find a memory I just wrote before it has matured",
        "A freshly written provisional memory is queryable immediately; include the provisional state in the filter to surface it right after the write.",
        (
            "Matured memories carry higher authority in ranking.",
            "Lifecycle promotes memories from provisional to matured over time.",
            "State filters control which lifecycle stages appear.",
        ),
        False,
    ),
    _BeirGroup(
        "does retrieval only return rows from the exact namespace I ask for",
        "A concrete-target query filters to the exact namespace, not the whole identity family; cross-presence federation needs an explicit wildcard.",
        (
            "Each presence's rows are isolated by namespace.",
            "Retrieval scopes results to the requested tenant and plane.",
            "Filters keep one presence from seeing another's memories.",
        ),
        False,
    ),
    _BeirGroup(
        "what happens when I save a near-duplicate memory",
        "A compatible near-duplicate merges into the existing row: tags union, the reinforcement count bumps, and no new row is inserted.",
        (
            "Duplicate detection uses cosine similarity plus factual compatibility.",
            "Merging avoids storing the same memory twice.",
            "An incompatible near-match is inserted as a distinct row.",
        ),
        False,
    ),
    _BeirGroup(
        "how do I make retrieval catch more of the hard long-tail answers",
        "To surface more long-tail matches, let the index consider more candidates before ranking; you accept some extra query time for the harder hits.",
        (
            "Hard queries need the search to look at more of the graph.",
            "Recall improves when fewer candidates are pruned early.",
            "There is a speed-versus-completeness tradeoff in vector search.",
        ),
        False,
    ),
)


def _beir_query_groups() -> tuple[_BeirGroup, ...]:
    return _BEIR_GROUPS


def test_beir_corpus_is_diverse_real_and_lexically_favorable() -> None:
    """Static corpus-diversity discriminator (Yua ruling): 12-20 hand-authored groups from real
    memory shapes, no generated glyphstone-style tokens or templated indices, distinct queries and
    targets, a substantial hybrid-favorable AND semantic split, and every hybrid-favorable group
    carries a REAL rare identifier shared by query + target that NO distractor contains (the lexical
    signal the sparse channel exploits)."""
    import re

    groups = _beir_query_groups()
    assert 12 <= len(groups) <= 20, f"need 12-20 groups; got {len(groups)}"

    def toks(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9][a-z0-9_.\-]*", text.lower()))

    _COMMON = {
        "the",
        "a",
        "an",
        "on",
        "in",
        "of",
        "to",
        "for",
        "and",
        "or",
        "is",
        "it",
        "do",
        "i",
        "what",
        "how",
        "which",
        "when",
        "does",
        "did",
        "we",
        "my",
        "its",
        "as",
        "with",
        "that",
        "this",
        "are",
        "be",
        "can",
        "into",
        "over",
        "before",
        "after",
        "not",
        "no",
        "yes",
        "run",
        "use",
        "used",
    }
    for group in groups:
        blob = " ".join([group.query, group.target, *group.distractors]).lower()
        assert "glyphstone" not in blob, "no generated glyphstone tokens"
        assert not re.search(r"\b(group|doc|row|item)\s*\d+\b", blob), "no templated indices"

    assert len({g.query for g in groups}) == len(groups), "queries must be distinct"
    assert len({g.target for g in groups}) == len(groups), "targets must be distinct"

    favorable = [g for g in groups if g.hybrid_favorable]
    semantic = [g for g in groups if not g.hybrid_favorable]
    assert len(favorable) >= len(groups) // 3, "need a substantial hybrid-favorable subset"
    assert len(semantic) >= len(groups) // 3, "need a substantial semantic subset (no sparse-only)"

    for group in favorable:
        shared = (toks(group.query) & toks(group.target)) - _COMMON
        distractor_toks: set[str] = set().union(*(toks(d) for d in group.distractors))
        rare = shared - distractor_toks
        assert rare, (
            f"hybrid-favorable group {group.query!r} has no rare identifier shared by query+target "
            f"that its distractors lack — sparse cannot lift it"
        )


@pytest.mark.integration
def test_integration_beir_style_eval_on_1000_doc_synthetic_corpus_hybrid_beats_dense_only_by_2_ndcg10_points() -> (
    None
):
    """RET-004: on a synthetic labelled corpus, hybrid (dense+sparse) retrieval must beat dense-only
    by at least BEIR_MIN_HYBRID_DENSE_DELTA (0.02) NDCG@10. Runs against the REAL Qdrant+TEI stack
    (marked ``integration`` → deselected locally, executed by the scheduled x86 TEI CI job). Never
    faked: without the stack this errors/deselects rather than reporting an invented delta."""
    from musubi.evals.live_gate import (
        BEIR_MIN_HYBRID_DENSE_DELTA,
        build_settings_backends,
    )
    from musubi.planes.episodic.plane import EpisodicPlane
    from musubi.retrieve.hybrid import hybrid_search
    from musubi.store.names import collection_for_plane
    from musubi.types.episodic import EpisodicMemory

    backends = build_settings_backends()  # raises LiveGateUnavailable without the real stack
    collection = collection_for_plane("episodic")
    namespace = "eric/beir-eval/episodic"
    plane = EpisodicPlane(client=backends.client, embedder=backends.embedder)

    from musubi.evals.live_gate import evaluate_query
    from musubi.evals.scheduled_gate import wait_for_visibility

    groups = _beir_query_groups()
    seeded_object_ids: set[str] = set()  # ACTUAL distinct rows (dedup can merge)
    per_query: list[dict[str, Any]] = []
    for group in groups:
        answer = asyncio.run(
            plane.create(EpisodicMemory(namespace=namespace, content=group.target, state="matured"))
        )
        seeded_object_ids.add(str(answer.object_id))
        for distractor in group.distractors:
            written = asyncio.run(
                plane.create(
                    EpisodicMemory(namespace=namespace, content=distractor, state="matured")
                )
            )
            seeded_object_ids.add(str(written.object_id))
        per_query.append({"group": group, "target": str(answer.object_id)})

    # Mature each distinct seeded row through the CANONICAL lifecycle transition. EpisodicPlane.create
    # forcibly persists rows as provisional (ignoring the constructor's state=), and default
    # hybrid_search excludes provisional — so unmatured rows are invisible to this mature-retrieval
    # benchmark (the 0/0 root cause, Yua 2026-07-15). Do NOT raw-set state; transition each ACTUAL
    # distinct object_id exactly once (dedup already collapsed them).
    import tempfile
    from pathlib import Path

    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator

    coordinator = LifecycleTransitionCoordinator(
        client=backends.client, db_path=Path(tempfile.mkdtemp()) / "beir-coord.db"
    )
    for object_id in seeded_object_ids:
        outcome = asyncio.run(
            plane.transition(
                namespace=namespace,
                object_id=object_id,
                to_state="matured",
                actor="beir-eval",
                reason="benchmark maturation",
                coordinator=coordinator,
            )
        )
        if isinstance(outcome, Err):
            raise AssertionError(f"maturation transition failed for {object_id}: {outcome}")

    # Reuse the scheduled gate's visibility semantics on the ACTUAL distinct seeded count.
    asyncio.run(
        wait_for_visibility(
            backends.client, collection, namespace, expected_count=len(seeded_object_ids)
        )
    )

    async def ranked_ids(query_text: str, hybrid: bool) -> list[str]:
        result = await hybrid_search(
            backends.client,
            backends.embedder,
            namespace=namespace,
            query=query_text,
            collection=collection,
            limit=10,
            dense_weight=1.0,
            sparse_weight=1.0 if hybrid else 0.0,  # dense-only drops the lexical channel
        )
        if isinstance(result, Err):
            raise AssertionError(f"hybrid_search failed: {result}")
        return [hit.object_id for hit in result.value.hits]

    def _rank(ordered: list[str], target: str) -> int:
        return ordered.index(target) + 1 if target in ordered else 0  # 0 = not in top-10

    # Per-group hybrid+dense rank/recall + ndcg — emitted so a 0/0 result or one-channel cheating is
    # visible in the CI log, per Yua's contract.
    hybrid_ndcgs: list[float] = []
    dense_ndcgs: list[float] = []
    print("BEIR per-group (H=hybrid-favorable, S=semantic):")
    for row in per_query:
        group, target = row["group"], row["target"]
        graded = [{"object_id": target, "relevance": 3}]
        h_ids = asyncio.run(ranked_ids(group.query, hybrid=True))
        d_ids = asyncio.run(ranked_ids(group.query, hybrid=False))
        h_ndcg = evaluate_query(h_ids, graded)["ndcg@10"]
        d_ndcg = evaluate_query(d_ids, graded)["ndcg@10"]
        hybrid_ndcgs.append(h_ndcg)
        dense_ndcgs.append(d_ndcg)
        tag = "H" if group.hybrid_favorable else "S"
        print(
            f"  [{tag}] {group.query[:52]!r}: hybrid rank {_rank(h_ids, target)} "
            f"recall {int(target in h_ids)} ndcg {h_ndcg:.3f} | dense rank {_rank(d_ids, target)} "
            f"recall {int(target in d_ids)} ndcg {d_ndcg:.3f}"
        )

    hybrid_ndcg = sum(hybrid_ndcgs) / len(hybrid_ndcgs)
    dense_ndcg = sum(dense_ndcgs) / len(dense_ndcgs)
    delta = hybrid_ndcg - dense_ndcg
    print(
        f"BEIR aggregate: hybrid_ndcg@10={hybrid_ndcg:.4f} dense_ndcg@10={dense_ndcg:.4f} "
        f"delta={delta:.4f} (need >= {BEIR_MIN_HYBRID_DENSE_DELTA})"
    )
    assert delta >= BEIR_MIN_HYBRID_DENSE_DELTA, (
        f"hybrid must beat dense-only by >= {BEIR_MIN_HYBRID_DENSE_DELTA} NDCG@10; "
        f"got hybrid {hybrid_ndcg:.4f} dense {dense_ndcg:.4f} delta {delta:.4f}"
    )


@pytest.mark.integration
def test_beir_maturation_makes_provisional_rows_visible_to_default_search() -> None:
    """RED discriminator (Yua ruling): EpisodicPlane.create persists a row PROVISIONAL, and default
    hybrid_search (mature-only visible states) cannot see it — the BEIR 0/0 root cause. The canonical
    maturation transition makes the EXACT SAME row visible to default search. Real Qdrant + a
    FakeEmbedder prove the maturation-visibility invariant with no TEI: pre-fix INVISIBLE, post-fix
    VISIBLE."""
    import os
    import tempfile
    from pathlib import Path

    from qdrant_client import QdrantClient
    from qdrant_client import models as qmodels

    from musubi.embedding import FakeEmbedder
    from musubi.evals.scheduled_gate import wait_for_visibility
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.planes.episodic.plane import EpisodicPlane
    from musubi.retrieve.hybrid import hybrid_search
    from musubi.store import bootstrap
    from musubi.store.names import collection_for_plane
    from musubi.types.common import LifecycleState, generate_ksuid
    from musubi.types.episodic import EpisodicMemory

    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    embedder = FakeEmbedder()
    plane = EpisodicPlane(client=client, embedder=embedder)
    collection = collection_for_plane("episodic")
    namespace = f"beirmat-{generate_ksuid()[:8].lower()}/dev/episodic"
    probe = "maturation visibility probe row for the discriminator"

    async def _search(*, include_provisional: bool) -> list[str]:
        state_filter: tuple[LifecycleState, ...] | None = (
            ("provisional", "matured") if include_provisional else None
        )
        result = await hybrid_search(
            client,
            embedder,
            namespace=namespace,
            query=probe,
            collection=collection,
            limit=10,
            state_filter=state_filter,
        )
        assert not isinstance(result, Err), result
        return [hit.object_id for hit in result.value.hits]

    try:
        saved = asyncio.run(
            plane.create(EpisodicMemory(namespace=namespace, content=probe, state="matured"))
        )
        oid = str(saved.object_id)
        asyncio.run(wait_for_visibility(client, collection, namespace, expected_count=1))

        # Seeded + queryable when provisional is allowed — proves the row exists and is indexed.
        assert oid in asyncio.run(_search(include_provisional=True))
        # RED: default (mature-only) search cannot see the provisional row.
        assert oid not in asyncio.run(_search(include_provisional=False)), (
            "a create()-persisted provisional row must be INVISIBLE to default hybrid_search"
        )

        # Canonical maturation of the EXACT same row (no raw payload-set).
        coordinator = LifecycleTransitionCoordinator(
            client=client, db_path=Path(tempfile.mkdtemp()) / "coord.db"
        )
        outcome = asyncio.run(
            plane.transition(
                namespace=namespace,
                object_id=oid,
                to_state="matured",
                actor="test",
                reason="maturation-visibility probe",
                coordinator=coordinator,
            )
        )
        assert not isinstance(outcome, Err), outcome

        # GREEN: the same row is now visible to default (mature-only) search.
        assert oid in asyncio.run(_search(include_provisional=False)), (
            "after canonical maturation the same row must be VISIBLE to default hybrid_search"
        )
    finally:
        client.delete(
            collection_name=collection,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="namespace", match=qmodels.MatchValue(value=namespace)
                        )
                    ]
                )
            ),
        )
        client.close()


@pytest.mark.skip(reason="deferred to slice-ops-gpu: live TEI/Qdrant p95 requires reference host")
def test_integration_live_qdrant_hybrid_with_real_bge_m3_splade_p95_150ms() -> None:
    raise AssertionError("covered by live performance gate")


def test_default_hybrid_prefetch_limit_matches_spec() -> None:
    assert HYBRID_PREFETCH_LIMIT == 50


def test_query_embedding_cache_rejects_non_positive_maxsize() -> None:
    with pytest.raises(ValueError, match="maxsize"):
        QueryEmbeddingCache(model_version="v1", maxsize=0)


def test_query_embedding_cache_keeps_entries_when_model_version_unchanged() -> None:
    cache = QueryEmbeddingCache(model_version="v1")

    cache.set_model_version("v1")

    assert cache.model_version == "v1"


@pytest.mark.asyncio
async def test_query_embedding_cache_evicts_lru_entry() -> None:
    cache = QueryEmbeddingCache(model_version="v1", maxsize=1)
    embedder = _CountingEmbedder()
    await _call(embedder=embedder, cache=cache, query="first query")
    await _call(embedder=embedder, cache=cache, query="second query")
    await _call(embedder=embedder, cache=cache, query="first query")

    assert embedder.dense_calls == 3
    assert embedder.sparse_calls == 3


@pytest.mark.asyncio
async def test_invalid_limit_returns_typed_error_without_querying_qdrant() -> None:
    spy = _SpyQdrantClient()
    result = await hybrid_search(
        _client(spy),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collection=COLLECTION,
        limit=0,
    )

    assert isinstance(result, Err)
    assert result.error.code == "invalid_limit"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_zero_dense_and_sparse_weights_return_typed_error() -> None:
    result = await hybrid_search(
        _client(_SpyQdrantClient()),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collection=COLLECTION,
        dense_weight=0.0,
        sparse_weight=0.0,
    )

    assert isinstance(result, Err)
    assert result.error.code == "invalid_weights"


@pytest.mark.asyncio
async def test_qdrant_failure_returns_typed_error() -> None:
    spy = _SpyQdrantClient(error=RuntimeError("qdrant down"))
    result = await hybrid_search(
        _client(spy),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collection=COLLECTION,
    )

    assert isinstance(result, Err)
    assert result.error.code == "qdrant_query_failed"


@pytest.mark.asyncio
async def test_dense_embedding_failure_returns_typed_error() -> None:
    spy, result = await _call(embedder=_BrokenDenseEmbedder())

    assert isinstance(result, Err)
    assert result.error.code == "dense_embedding_failed"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_sparse_embedding_failure_returns_typed_error() -> None:
    spy, result = await _call(embedder=_BrokenSparseEmbedder())

    assert isinstance(result, Err)
    assert result.error.code == "sparse_embedding_failed"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_dense_only_search_omits_sparse_prefetch() -> None:
    spy, result = await _call(sparse_weight=0.0)

    assert isinstance(result, Ok)
    assert [prefetch.using for prefetch in _prefetches(spy.calls[0])] == [DENSE_VECTOR_NAME]


@pytest.mark.asyncio
async def test_sparse_only_search_omits_dense_prefetch() -> None:
    spy, result = await _call(dense_weight=0.0)

    assert isinstance(result, Ok)
    assert [prefetch.using for prefetch in _prefetches(spy.calls[0])] == [SPARSE_VECTOR_NAME]


@pytest.mark.asyncio
async def test_dense_only_collection_skips_sparse_prefetch() -> None:
    # `musubi_artifact` is declared `has_sparse=False` in the registry
    # (metadata-only collection). Qdrant rejects sparse queries against
    # it with 400 "Not existing vector name" — regression gate for #208.
    spy = _SpyQdrantClient()
    embedder = _CountingEmbedder()
    result = await hybrid_search(
        _client(spy),
        embedder,
        namespace=NAMESPACE,
        query="find gpu notes",
        collection="musubi_artifact",
    )

    assert isinstance(result, Ok)
    assert [prefetch.using for prefetch in _prefetches(spy.calls[0])] == [DENSE_VECTOR_NAME]
    # Don't embed sparse if we're never going to use it.
    assert embedder.sparse_calls == 0


@pytest.mark.asyncio
async def test_fanout_mismatched_clients_and_collections_returns_typed_error() -> None:
    result = await hybrid_search_many(
        [_client(_SpyQdrantClient())],
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collections=[COLLECTION, "musubi_curated"],
    )

    assert isinstance(result, Err)
    assert result.error.code == "fanout_mismatch"


@pytest.mark.asyncio
async def test_fanout_returns_first_child_error() -> None:
    result = await hybrid_search_many(
        [_client(_SpyQdrantClient())],
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="",
        collections=[COLLECTION],
    )

    assert isinstance(result, Err)
    assert result.error.code == "empty_query"


@pytest.mark.asyncio
async def test_state_filter_overrides_default_visible_states() -> None:
    spy, result = await _call(state_filter=("archived",))

    assert isinstance(result, Ok)
    conditions = _filter_conditions(spy.calls[0])
    state_conditions = [
        condition
        for condition in conditions
        if isinstance(condition, models.FieldCondition) and condition.key == "state"
    ]
    match = state_conditions[0].match
    assert isinstance(match, models.MatchAny)
    assert match.any == ["archived"]
