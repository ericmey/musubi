"""Test contract for slice-embedding.

Covers the Embedder protocol, the three TEI HTTP clients, and the in-process
``FakeEmbedder`` test double. Tests in this file are unit-level; they mock the
TEI endpoints with ``pytest-httpx`` rather than standing up a live TEI.

Contract (from the spec + the slice brief):

1. ``embed_dense`` returns one dense vector per input at the correct
   dimensionality (``DENSE_SIZE`` from ``musubi.store.specs``).
2. ``embed_sparse`` returns one ``dict[int, float]`` per input (index →
   weight).
3. ``rerank`` returns one float score per candidate, in candidate order.
4. ``FakeEmbedder`` is deterministic — the same input yields the same output.
5. ``FakeEmbedder`` dense outputs are unit-normed so cosine distance
   behaves sensibly.
6. TEI clients POST the payload shape TEI expects (``{"inputs": [...]}``
   for encoders, ``{"query": ..., "texts": [...]}`` for rerank).
7. TEI clients raise a typed :class:`EmbeddingError` on 5xx responses
   instead of propagating ``httpx.HTTPStatusError``.
8. Concurrent batching preserves input order in the output list.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator

import httpx
import pytest
from pytest_httpx import HTTPXMock

from musubi.embedding import (
    CachedEmbedder,
    Embedder,
    EmbeddingError,
    FakeEmbedder,
    TEIDenseClient,
    TEIRerankerClient,
    TEISparseClient,
)
from musubi.store.specs import DENSE_SIZE

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake() -> AsyncIterator[FakeEmbedder]:
    embedder = FakeEmbedder(dense_size=DENSE_SIZE)
    yield embedder


# ---------------------------------------------------------------------------
# 1. embed_dense dimensionality
# ---------------------------------------------------------------------------


async def test_embed_dense_returns_correct_dimensionality(fake: FakeEmbedder) -> None:
    vectors = await fake.embed_dense(["hello", "world"])
    assert len(vectors) == 2
    for v in vectors:
        assert len(v) == DENSE_SIZE
        assert all(isinstance(x, float) for x in v)


async def test_encode_dense_returns_1024_dim(fake: FakeEmbedder) -> None:
    [vector] = await fake.embed_dense(["hello"])
    assert len(vector) == DENSE_SIZE


# ---------------------------------------------------------------------------
# 2. embed_sparse shape
# ---------------------------------------------------------------------------


async def test_embed_sparse_returns_dict_index_to_weight(fake: FakeEmbedder) -> None:
    result = await fake.embed_sparse(["hello world"])
    assert len(result) == 1
    vec = result[0]
    assert isinstance(vec, dict)
    assert vec  # non-empty for non-empty input
    for idx, weight in vec.items():
        assert isinstance(idx, int)
        assert isinstance(weight, float)
        assert weight >= 0.0


async def test_encode_sparse_returns_nonempty_dict(fake: FakeEmbedder) -> None:
    [vector] = await fake.embed_sparse(["hello world"])
    assert vector
    assert all(isinstance(index, int) for index in vector)


# ---------------------------------------------------------------------------
# 3. rerank
# ---------------------------------------------------------------------------


async def test_rerank_returns_score_per_candidate(fake: FakeEmbedder) -> None:
    scores = await fake.rerank("query", ["candidate 1", "candidate 2", "candidate 3"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)


async def test_encode_parallel_dense_sparse(fake: FakeEmbedder) -> None:
    dense, sparse = await asyncio.gather(
        fake.embed_dense(["parallel"]),
        fake.embed_sparse(["parallel"]),
    )
    assert len(dense[0]) == DENSE_SIZE
    assert sparse[0]


# ---------------------------------------------------------------------------
# 4 + 5. FakeEmbedder determinism & unit norm
# ---------------------------------------------------------------------------


async def test_fake_embedder_is_deterministic(fake: FakeEmbedder) -> None:
    a = await fake.embed_dense(["musubi"])
    b = await fake.embed_dense(["musubi"])
    assert a == b
    c = await fake.embed_sparse(["musubi"])
    d = await fake.embed_sparse(["musubi"])
    assert c == d


async def test_fake_embedder_dense_has_unit_norm(fake: FakeEmbedder) -> None:
    [vec] = await fake.embed_dense(["unit-norm-me"])
    norm = math.sqrt(sum(x * x for x in vec))
    assert math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6)


async def test_fake_embedder_different_inputs_differ(fake: FakeEmbedder) -> None:
    a = await fake.embed_dense(["alpha"])
    b = await fake.embed_dense(["beta"])
    assert a != b


# ---------------------------------------------------------------------------
# 6. TEI clients post the correct payload shape
# ---------------------------------------------------------------------------


async def test_tei_dense_client_posts_correct_payload_shape(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[0.0] * DENSE_SIZE, [0.0] * DENSE_SIZE],
    )
    client = TEIDenseClient(base_url="http://tei-dense")
    out = await client.embed_dense(["a", "b"])
    assert len(out) == 2
    req = httpx_mock.get_request()
    assert req is not None
    body = req.content.decode()
    assert '"inputs"' in body
    assert '"a"' in body and '"b"' in body


async def test_tei_sparse_client_posts_correct_payload_shape(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-sparse/embed_sparse",
        method="POST",
        json=[[{"index": 3, "value": 0.8}, {"index": 7, "value": 0.5}]],
    )
    client = TEISparseClient(base_url="http://tei-sparse")
    out = await client.embed_sparse(["hello"])
    assert out == [{3: 0.8, 7: 0.5}]


async def test_tei_reranker_client_posts_correct_payload_shape(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-reranker/rerank",
        method="POST",
        json=[
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.3},
            {"index": 2, "score": 0.7},
        ],
    )
    client = TEIRerankerClient(base_url="http://tei-reranker")
    scores = await client.rerank("q", ["a", "b", "c"])
    # Scores must be returned in *candidate order* (index 0, 1, 2) regardless
    # of the order the server answered in.
    assert scores == [0.9, 0.3, 0.7]
    req = httpx_mock.get_request()
    assert req is not None
    body = req.content.decode()
    assert '"query"' in body and '"texts"' in body


# ---------------------------------------------------------------------------
# 7. Typed error on 5xx
# ---------------------------------------------------------------------------


async def test_tei_dense_client_raises_typed_error_on_5xx(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        status_code=503,
        text="inference backend unavailable",
    )
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        status_code=503,
        text="inference backend unavailable",
    )
    client = TEIDenseClient(base_url="http://tei-dense", retry_backoff=0.0)
    with pytest.raises(EmbeddingError) as excinfo:
        await client.embed_dense(["x"])
    # Error carries the status and a useful message — not just "HTTPError".
    assert excinfo.value.status_code == 503
    assert "unavailable" in str(excinfo.value).lower() or "503" in str(excinfo.value)


async def test_tei_sparse_client_raises_typed_error_on_5xx(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-sparse/embed_sparse",
        method="POST",
        status_code=500,
        text="boom",
    )
    httpx_mock.add_response(
        url="http://tei-sparse/embed_sparse",
        method="POST",
        status_code=500,
        text="boom",
    )
    client = TEISparseClient(base_url="http://tei-sparse", retry_backoff=0.0)
    with pytest.raises(EmbeddingError):
        await client.embed_sparse(["x"])


async def test_tei_reranker_client_raises_typed_error_on_5xx(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://tei-reranker/rerank",
        method="POST",
        status_code=502,
        text="bad gateway",
    )
    httpx_mock.add_response(
        url="http://tei-reranker/rerank",
        method="POST",
        status_code=502,
        text="bad gateway",
    )
    client = TEIRerankerClient(base_url="http://tei-reranker", retry_backoff=0.0)
    with pytest.raises(EmbeddingError):
        await client.rerank("q", ["c"])


# ---------------------------------------------------------------------------
# 8. Concurrent batching preserves order
# ---------------------------------------------------------------------------


async def test_concurrent_batching_preserves_order(httpx_mock: HTTPXMock) -> None:
    # Server returns one vector per input; index 0 has dense=[1,...], index 1
    # has dense=[2,...], etc. The client must keep the server's answer in the
    # same order as the client's input list.
    n = 5
    response: list[list[float]] = [[float(i)] + [0.0] * (DENSE_SIZE - 1) for i in range(n)]
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=response,
    )
    client = TEIDenseClient(base_url="http://tei-dense")
    out = await client.embed_dense([f"text-{i}" for i in range(n)])
    for i, vec in enumerate(out):
        assert vec[0] == float(i)


async def test_fake_batch_preserves_order(fake: FakeEmbedder) -> None:
    inputs = [f"item-{i}" for i in range(20)]
    batch = await fake.embed_dense(inputs)
    per_item = [(await fake.embed_dense([s]))[0] for s in inputs]
    assert batch == per_item


async def test_batch_encode_64_items_one_call(httpx_mock: HTTPXMock) -> None:
    texts = [f"item-{i}" for i in range(64)]
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[float(i)] + [0.0] * (DENSE_SIZE - 1) for i in range(64)],
    )

    client = TEIDenseClient(base_url="http://tei-dense", max_batch_size=64)
    out = await client.embed_dense(texts)

    assert len(out) == 64
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].read().decode().count("item-") == 64


async def test_batch_encode_above_64_chunks_requests(httpx_mock: HTTPXMock) -> None:
    texts = [f"item-{i}" for i in range(65)]
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[float(i)] + [0.0] * (DENSE_SIZE - 1) for i in range(64)],
    )
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[64.0] + [0.0] * (DENSE_SIZE - 1)],
    )

    client = TEIDenseClient(base_url="http://tei-dense", max_batch_size=64)
    out = await client.embed_dense(texts)

    assert len(out) == 65
    assert [vec[0] for vec in out] == [float(i) for i in range(65)]
    assert len(httpx_mock.get_requests()) == 2


async def test_truncate_content_to_2048_chars(httpx_mock: HTTPXMock) -> None:
    long_text = "x" * 2050
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[0.0] * DENSE_SIZE],
    )

    client = TEIDenseClient(base_url="http://tei-dense")
    await client.embed_dense([long_text])

    request = httpx_mock.get_request()
    assert request is not None
    payload = request.content.decode()
    assert "x" * 2048 in payload
    assert "x" * 2049 not in payload


async def test_query_cache_hit_on_repeat() -> None:
    wrapped = CountingEmbedder()
    cached = CachedEmbedder(wrapped, model_revision="dense-v1")

    first = await cached.embed_dense(["same query"])
    second = await cached.embed_dense(["same query"])

    assert first == second
    assert wrapped.dense_calls == 1


async def test_query_cache_miss_on_different_query() -> None:
    wrapped = CountingEmbedder()
    cached = CachedEmbedder(wrapped, model_revision="dense-v1")

    await cached.embed_dense(["alpha"])
    await cached.embed_dense(["beta"])

    assert wrapped.dense_calls == 2


async def test_query_cache_cleared_on_model_revision_change() -> None:
    wrapped = CountingEmbedder()
    cached = CachedEmbedder(wrapped, model_revision="dense-v1")

    first = await cached.embed_dense(["same query"])
    cached.set_model_revision("dense-v2")
    second = await cached.embed_dense(["same query"])

    assert first != second
    assert wrapped.dense_calls == 2


async def test_tei_transient_5xx_retries_once(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        status_code=503,
        text="warming",
    )
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[1.0] + [0.0] * (DENSE_SIZE - 1)],
    )

    client = TEIDenseClient(base_url="http://tei-dense", retry_backoff=0.0)
    out = await client.embed_dense(["x"])

    assert out[0][0] == 1.0
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.skip(
    reason="deferred to slice-retrieval-hybrid: Qdrant upsert lives outside embedding"
)
def test_upsert_specifies_both_named_vectors() -> None:
    raise AssertionError("covered by retrieval/store integration follow-up")


@pytest.mark.skip(
    reason="deferred to slice-retrieval-hybrid: Qdrant query construction lives outside embedding"
)
def test_query_uses_specified_named_vector() -> None:
    raise AssertionError("covered by retrieval/store integration follow-up")


@pytest.mark.skip(
    reason="deferred to slice-reembedding-migration: migration vector add/rebuild lives outside embedding"
)
def test_collection_can_add_new_named_vector_without_rebuild() -> None:
    raise AssertionError("covered by re-embedding migration follow-up")


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: body_hash re-embed decisions live outside embedding"
)
def test_body_hash_unchanged_skips_reembed() -> None:
    raise AssertionError("covered by vault-sync follow-up")


@pytest.mark.skip(
    reason="deferred to slice-vault-sync: body_hash re-embed decisions live outside embedding"
)
def test_body_hash_changed_triggers_reembed() -> None:
    raise AssertionError("covered by vault-sync follow-up")


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: synthesis reinforcement lives outside embedding"
)
def test_synthesis_reinforce_does_not_reembed() -> None:
    raise AssertionError("covered by lifecycle follow-up")


@pytest.mark.skip(
    reason="deferred to slice-api-v0: capture endpoint 503 mapping lives outside embedding"
)
def test_tei_down_capture_returns_503() -> None:
    raise AssertionError("covered by API follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ingestion-capture: sequential fallback policy lives outside TEI client"
)
def test_tei_timeout_on_batch_falls_back_to_sequential() -> None:
    raise AssertionError("covered by ingestion capture follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ops-compose: service health budgets require Docker Compose"
)
def test_all_four_services_healthy_within_60s() -> None:
    raise AssertionError("covered by ops integration follow-up")


@pytest.mark.skip(reason="deferred to slice-ops-gpu: VRAM budget requires reference GPU host")
def test_vram_below_9_5gb_after_cold_start() -> None:
    raise AssertionError("covered by ops GPU follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ops-observability: latency budgets require live TEI dense service"
)
def test_tei_dense_encode_latency_p95_lt_50ms() -> None:
    raise AssertionError("covered by ops performance follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ops-observability: latency budgets require live TEI sparse service"
)
def test_tei_sparse_encode_latency_p95_lt_80ms() -> None:
    raise AssertionError("covered by ops performance follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ops-observability: latency budgets require live reranker service"
)
def test_reranker_40pair_batch_p95_lt_200ms() -> None:
    raise AssertionError("covered by ops performance follow-up")


@pytest.mark.skip(
    reason="deferred to slice-ops-observability: latency budgets require live Ollama service"
)
def test_ollama_qwen25_generation_p50_lt_4s_for_200_token_output() -> None:
    raise AssertionError("covered by ops performance follow-up")


@pytest.mark.skip(
    reason="deferred to slice-retrieval-hybrid: Ollama degradation belongs to retrieval/core"
)
def test_core_degrades_gracefully_if_ollama_killed() -> None:
    raise AssertionError("covered by retrieval/core integration follow-up")


@pytest.mark.skip(reason="deferred to slice-api-v0: TEI outage-to-503 mapping belongs to API/core")
def test_core_503s_if_tei_dense_killed() -> None:
    raise AssertionError("covered by API/core integration follow-up")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


async def test_fake_embedder_satisfies_protocol(fake: FakeEmbedder) -> None:
    assert isinstance(fake, Embedder)


def test_tei_clients_can_stand_alone() -> None:
    """Each TEI client is independent — constructing one does not require
    the others (they target different URLs)."""
    TEIDenseClient(base_url="http://tei-dense")
    TEISparseClient(base_url="http://tei-sparse")
    TEIRerankerClient(base_url="http://tei-reranker")


class CountingEmbedder(Embedder):
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_calls = 0

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.dense_calls += 1
        return [
            [float(self.dense_calls), float(len(text)), *([0.0] * (DENSE_SIZE - 2))]
            for text in texts
        ]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.sparse_calls += 1
        return [{self.sparse_calls: float(len(text))} for text in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [float(len(query) + len(candidate)) for candidate in candidates]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_embed_dense_empty_input_returns_empty(fake: FakeEmbedder) -> None:
    assert await fake.embed_dense([]) == []


async def test_embed_sparse_empty_input_returns_empty(fake: FakeEmbedder) -> None:
    assert await fake.embed_sparse([]) == []


async def test_rerank_empty_candidates_returns_empty(fake: FakeEmbedder) -> None:
    assert await fake.rerank("q", []) == []


async def test_tei_dense_client_passes_custom_timeout(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://tei-dense/embed",
        method="POST",
        json=[[0.0] * DENSE_SIZE],
    )
    client = TEIDenseClient(base_url="http://tei-dense", timeout=5.0)
    # Smoke: doesn't crash when timeout is explicitly set.
    out = await client.embed_dense(["x"])
    assert len(out) == 1


async def test_tei_dense_client_handles_httpx_connect_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    client = TEIDenseClient(base_url="http://tei-dense")
    with pytest.raises(EmbeddingError):
        await client.embed_dense(["x"])


# ---------------------------------------------------------------------------
# Connection pooling — regression guard for the "fresh AsyncClient per call"
# anti-pattern that showed up as 10.94% failure rate under Gate 2 load.
# Each client must own one long-lived httpx.AsyncClient and reuse it across
# calls so HTTP/1.1 keepalive actually kicks in.
# ---------------------------------------------------------------------------


async def test_tei_dense_client_reuses_single_async_client(
    httpx_mock: HTTPXMock,
) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            url="http://tei-dense/embed",
            method="POST",
            json=[[0.0] * DENSE_SIZE],
        )
    client = TEIDenseClient(base_url="http://tei-dense")
    first = client._client
    for _ in range(3):
        await client.embed_dense(["x"])
    # Same AsyncClient instance across calls → keepalive + pooling in play.
    assert client._client is first


async def test_tei_sparse_client_reuses_single_async_client(
    httpx_mock: HTTPXMock,
) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            url="http://tei-sparse/embed_sparse",
            method="POST",
            json=[[{"index": 0, "value": 1.0}]],
        )
    client = TEISparseClient(base_url="http://tei-sparse")
    first = client._client
    for _ in range(3):
        await client.embed_sparse(["x"])
    assert client._client is first


async def test_tei_reranker_client_reuses_single_async_client(
    httpx_mock: HTTPXMock,
) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            url="http://tei-reranker/rerank",
            method="POST",
            json=[{"index": 0, "score": 0.9}],
        )
    client = TEIRerankerClient(base_url="http://tei-reranker")
    first = client._client
    for _ in range(3):
        await client.rerank("q", ["c"])
    assert client._client is first


async def test_tei_dense_client_aclose_closes_pool() -> None:
    """aclose() must actually close the underlying httpx pool so process
    shutdown drains cleanly + tests don't leak ResourceWarnings."""
    client = TEIDenseClient(base_url="http://tei-dense")
    assert not client._client.is_closed
    await client.aclose()
    assert client._client.is_closed


async def test_tei_sparse_client_aclose_closes_pool() -> None:
    client = TEISparseClient(base_url="http://tei-sparse")
    await client.aclose()
    assert client._client.is_closed


async def test_tei_reranker_client_aclose_closes_pool() -> None:
    client = TEIRerankerClient(base_url="http://tei-reranker")
    await client.aclose()
    assert client._client.is_closed
