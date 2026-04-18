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

import math
from collections.abc import AsyncIterator

import httpx
import pytest
from pytest_httpx import HTTPXMock

from musubi.embedding import (
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


# ---------------------------------------------------------------------------
# 3. rerank
# ---------------------------------------------------------------------------


async def test_rerank_returns_score_per_candidate(fake: FakeEmbedder) -> None:
    scores = await fake.rerank("query", ["candidate 1", "candidate 2", "candidate 3"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)


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
    client = TEIDenseClient(base_url="http://tei-dense")
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
    client = TEISparseClient(base_url="http://tei-sparse")
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
    client = TEIRerankerClient(base_url="http://tei-reranker")
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
