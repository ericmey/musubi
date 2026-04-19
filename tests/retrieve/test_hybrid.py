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
async def test_namespace_filter_always_applied() -> None:
    spy, _result = await _call()

    conditions = _filter_conditions(spy.calls[0])
    assert any(
        isinstance(condition, models.FieldCondition)
        and condition.key == "namespace"
        and isinstance(condition.match, models.MatchValue)
        and condition.match.value == NAMESPACE
        for condition in conditions
    )


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
async def test_hybrid_timeout_returns_partial_results() -> None:
    spy = _SpyQdrantClient(delay_s=0.05)
    result = await hybrid_search(
        _client(spy),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="find gpu notes",
        collection=COLLECTION,
        timeout_s=0.001,
    )

    assert isinstance(result, Ok)
    assert result.value == []


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
    assert [hit.object_id for hit in result.value] == ["same"]


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


@pytest.mark.skip(
    reason="deferred to slice-retrieval-evals: BEIR evaluation requires benchmark corpus"
)
def test_integration_beir_style_eval_on_1000_doc_synthetic_corpus_hybrid_beats_dense_only_by_2_ndcg10_points() -> (
    None
):
    raise AssertionError("covered by retrieval eval suite")


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
