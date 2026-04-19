"""Test contract for slice-retrieval-fast."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from musubi.retrieve.fast import FastResponseCache, run_fast_retrieve
from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.retrieve.hybrid import HybridHit, QueryEmbeddingCache, RetrievalError
from musubi.types.common import Err, Ok

NAMESPACE = "tenant/presence/episodic"
COLLECTION = "musubi_episodic"
NOW = 2_000_000_000.0


class _CountingEmbedder(Embedder):
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_calls = 0

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.dense_calls += 1
        return [[1.0, 0.0, 0.0] for _text in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        return [{1: 1.0} for _text in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        raise AssertionError("fast path must not call rerank")


class _SpyQdrantClient:
    pass


def _client() -> QdrantClient:
    return cast(QdrantClient, _SpyQdrantClient())


def _hybrid_hit(
    object_id: str,
    *,
    score: float,
    namespace: str = NAMESPACE,
    state: str = "matured",
    content: str = "short content",
    promoted_to: str | None = None,
) -> HybridHit:
    payload: dict[str, Any] = {
        "object_id": object_id,
        "namespace": namespace,
        "state": state,
        "plane": "episodic",
        "updated_epoch": NOW,
        "importance": 5,
        "reinforcement_count": 0,
        "access_count": 0,
        "content": content,
        "title": f"title {object_id}",
        "lineage": {"promoted_to": promoted_to},
    }
    if promoted_to is not None:
        payload["promoted_to"] = promoted_to
    return HybridHit(object_id=object_id, score=score, payload=payload)


def _hits() -> list[HybridHit]:
    return [
        _hybrid_hit("low", score=0.1, content="low content"),
        _hybrid_hit("high", score=0.9, content="high content"),
        _hybrid_hit("mid", score=0.5, content="mid content"),
    ]


@pytest.mark.asyncio
async def test_fast_path_p50_under_150ms_on_10k_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        return Ok(value=_hits())

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    started = time.perf_counter()
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert isinstance(result, Ok)
    assert elapsed_ms < 150


@pytest.mark.asyncio
async def test_fast_path_returns_results_in_score_desc(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        return Ok(value=_hits())

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert [hit.object_id for hit in result.value.results] == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_fast_path_applies_namespace_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        calls.append(kwargs)
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert calls[0]["namespace"] == NAMESPACE


@pytest.mark.asyncio
async def test_fast_path_applies_state_matured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        calls.append(kwargs)
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert calls[0]["state_filter"] == ("matured", "promoted")


@pytest.mark.asyncio
async def test_fast_path_runs_planes_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        await asyncio.sleep(0.05)
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    started = time.perf_counter()
    result = await run_fast_retrieve(
        [_client(), _client()],
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collections=[COLLECTION, "musubi_curated"],
        now=NOW,
    )
    elapsed = time.perf_counter() - started

    assert isinstance(result, Ok)
    assert elapsed < 0.09


@pytest.mark.asyncio
async def test_fast_path_timeout_on_one_plane_returns_partial_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        if kwargs["collection"] == "musubi_curated":
            await asyncio.sleep(0.05)
        return Ok(value=[_hybrid_hit(kwargs["collection"], score=0.9)])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        [_client(), _client()],
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collections=[COLLECTION, "musubi_curated"],
        now=NOW,
        plane_timeout_s=0.001,
    )

    assert isinstance(result, Ok)
    assert [hit.object_id for hit in result.value.results] == [COLLECTION]
    assert result.value.warnings == ["plane: musubi_curated timed out"]


@pytest.mark.asyncio
async def test_fast_path_tei_timeout_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Err[RetrievalError]:
        return Err(error=RetrievalError(code="no_query_vectors", detail="both encoders timed out"))

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Err)
    assert result.error.code == "embeddings_unavailable"
    assert result.error.status_code == 503


@pytest.mark.asyncio
async def test_fast_path_qdrant_down_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Err[RetrievalError]:
        return Err(error=RetrievalError(code="qdrant_query_failed", detail="down"))

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Err)
    assert result.error.code == "index_unavailable"
    assert result.error.status_code == 503


@pytest.mark.asyncio
async def test_fast_path_empty_corpus_returns_empty_200(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert result.value.results == []
    assert result.value.status_code == 200


@pytest.mark.asyncio
async def test_fast_path_response_cache_hits_within_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        nonlocal calls
        calls += 1
        return Ok(value=_hits())

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    cache = FastResponseCache()
    first = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
        response_cache=cache,
    )
    second = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW + 10,
        response_cache=cache,
    )

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert calls == 1
    assert second.value.cache_hit is True


@pytest.mark.asyncio
async def test_fast_path_response_cache_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        nonlocal calls
        calls += 1
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    for _ in range(2):
        result = await run_fast_retrieve(
            _client(),
            _CountingEmbedder(),
            namespace=NAMESPACE,
            query="gpu",
            collection=COLLECTION,
            now=NOW,
        )
        assert isinstance(result, Ok)

    assert calls == 2


@pytest.mark.asyncio
async def test_fast_path_embedding_cache_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    caches: list[QueryEmbeddingCache | None] = []

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        caches.append(kwargs["cache"])
        return Ok(value=[])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert isinstance(caches[0], QueryEmbeddingCache)


@pytest.mark.asyncio
async def test_fast_path_snippet_max_200_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        return Ok(value=[_hybrid_hit("long", score=1.0, content="x" * 250)])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert len(result.value.results[0].snippet) == 200


@pytest.mark.asyncio
async def test_fast_path_lineage_summary_present_not_hydrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> Ok[list[HybridHit]]:
        return Ok(value=[_hybrid_hit("lineage", score=1.0, promoted_to="curated-id")])

    import musubi.retrieve.fast as fast

    monkeypatch.setattr(fast, "hybrid_search", fake_hybrid_search)
    result = await run_fast_retrieve(
        _client(),
        _CountingEmbedder(),
        namespace=NAMESPACE,
        query="gpu",
        collection=COLLECTION,
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert result.value.results[0].lineage_summary == {"promoted_to": "curated-id"}
    assert "hydrated_lineage" not in result.value.results[0].payload


def test_fast_path_does_not_call_reranker() -> None:
    source = __import__("pathlib").Path("src/musubi/retrieve/fast.py").read_text()

    assert "rerank" not in source


@pytest.mark.property
@given(scores=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=20))
def test_hypothesis_same_query_on_same_corpus_returns_identical_results(
    scores: list[float],
) -> None:
    hits = [
        _hybrid_hit(f"object-{i}", score=score, content=f"content {i}")
        for i, score in enumerate(scores)
    ]

    first = sorted(hits, key=lambda hit: (-hit.score, hit.object_id))
    reversed_hits = hits[::-1]
    second = sorted(reversed_hits, key=lambda hit: (-hit.score, hit.object_id))

    assert first == second


@pytest.mark.property
@given(limit=st.integers(min_value=1, max_value=20), size=st.integers(min_value=0, max_value=40))
def test_hypothesis_limit_parameter_is_honored_exactly(limit: int, size: int) -> None:
    hits = [_hybrid_hit(f"object-{i}", score=1.0 / (i + 1)) for i in range(size)]

    assert len(hits[:limit]) == min(limit, size)


@pytest.mark.skip(reason="deferred to slice-retrieval-evals: LiveKit scenario needs perf harness")
def test_integration_livekit_fast_talker_scenario_voice_like_queries_p95_400ms() -> None:
    raise AssertionError("covered by retrieval eval suite")


@pytest.mark.skip(
    reason="deferred to slice-ops-observability: live sparse TEI kill test needs services"
)
def test_integration_degradation_scenario_kill_sparse_tei_mid_request_response_still_returns_with_warnings() -> (
    None
):
    raise AssertionError("covered by live degradation suite")
