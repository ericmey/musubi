"""Test contract for :class:`ChunkedEmbedder`.

The wrapper enforces SPLADE-v3's 512-token input ceiling by sliding-window
chunking + max-pool aggregation. Tests cover:

1. Short inputs pass through with one downstream call (no aggregation).
2. Long inputs split into multiple chunks, max-pooled to one output vector
   per input.
3. Max-pool correctness on hand-crafted chunk vectors.
4. Mixed batches preserve input order and per-input vector identity.
5. Dense + rerank delegate unchanged regardless of input length.
6. Empty input returns empty output.

The wrapped embedder is a spy that records every call, so we can assert on
both the shape of the output AND the batching pattern (one flattened call
per :meth:`embed_sparse` invocation, regardless of input count).
"""

from __future__ import annotations

from musubi.embedding import ChunkedEmbedder, FakeEmbedder
from musubi.embedding.base import Embedder
from musubi.embedding.chunked import _max_pool_sparse


class _SpyEmbedder(Embedder):
    """Records each call so tests can assert on batching pattern.

    Delegates to :class:`FakeEmbedder` for actual vectors so outputs stay
    deterministic.
    """

    def __init__(self) -> None:
        self._inner = FakeEmbedder()
        self.embed_sparse_calls: list[list[str]] = []
        self.embed_dense_calls: list[list[str]] = []
        self.rerank_calls: list[tuple[str, list[str]]] = []

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        self.embed_dense_calls.append(list(texts))
        return await self._inner.embed_dense(texts)

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        self.embed_sparse_calls.append(list(texts))
        return await self._inner.embed_sparse(texts)

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.rerank_calls.append((query, list(candidates)))
        return await self._inner.rerank(query, candidates)


def _long_english_text(approx_tokens: int) -> str:
    """Build a deterministic English paragraph of roughly ``approx_tokens``
    BGE-M3 tokens. Uses a fixed sentence so tests are reproducible."""
    sentence = (
        "The quick brown fox jumps over the lazy dog while observability "
        "engineers debate retention policies and chunk-window overlaps. "
    )
    # ~22 BGE-M3 tokens per sentence in practice; over-shoot to be safe.
    n = max(1, approx_tokens // 18 + 2)
    return sentence * n


# ---------------------------------------------------------------------------
# 1. Short input pass-through
# ---------------------------------------------------------------------------


async def test_short_input_takes_single_chunk_path() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy)

    out = await embedder.embed_sparse(["hello world"])

    assert len(out) == 1
    assert isinstance(out[0], dict)
    # One downstream call, with one input — no fan-out.
    assert len(spy.embed_sparse_calls) == 1
    assert len(spy.embed_sparse_calls[0]) == 1


# ---------------------------------------------------------------------------
# 2. Long input chunked and pooled
# ---------------------------------------------------------------------------


async def test_long_input_chunked_into_multiple_windows() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy, sparse_window_tokens=64, sparse_overlap_tokens=8)

    # ~600 tokens worth of text; with window=64 / overlap=8, this guarantees
    # the chunker produces multiple windows. Window is shrunk for the test
    # so we don't have to construct genuinely SPLADE-overflowing text.
    text = _long_english_text(approx_tokens=600)

    out = await embedder.embed_sparse([text])

    assert len(out) == 1, "one input -> one output vector"
    assert isinstance(out[0], dict)
    # Downstream got >1 chunk in its one batched call.
    assert len(spy.embed_sparse_calls) == 1
    assert len(spy.embed_sparse_calls[0]) > 1, "long input should produce multiple chunks"


# ---------------------------------------------------------------------------
# 3. Max-pool correctness
# ---------------------------------------------------------------------------


def test_max_pool_empty() -> None:
    assert _max_pool_sparse([]) == {}


def test_max_pool_single_vector_returns_copy() -> None:
    vec = {5: 0.7, 11: 0.2}
    out = _max_pool_sparse([vec])
    assert out == vec
    assert out is not vec, "must not alias caller's dict"


def test_max_pool_takes_per_index_max() -> None:
    a = {5: 0.8, 9: 0.1}
    b = {5: 0.3, 7: 0.5, 9: 0.4}
    c = {7: 0.9, 11: 0.6}
    pooled = _max_pool_sparse([a, b, c])
    assert pooled == {5: 0.8, 7: 0.9, 9: 0.4, 11: 0.6}


# ---------------------------------------------------------------------------
# 4. Mixed batch — short + long together
# ---------------------------------------------------------------------------


async def test_mixed_batch_preserves_per_input_identity() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy, sparse_window_tokens=64, sparse_overlap_tokens=8)

    short = "tiny"
    long_text = _long_english_text(approx_tokens=600)

    out = await embedder.embed_sparse([short, long_text, "another short"])

    assert len(out) == 3, "output count must match input count"
    assert all(isinstance(v, dict) for v in out)

    # Exactly one downstream call, flattened across all inputs.
    assert len(spy.embed_sparse_calls) == 1
    flat = spy.embed_sparse_calls[0]
    # short -> 1 chunk, long -> N>1 chunks, short -> 1 chunk. Total > 3.
    assert len(flat) > 3
    # First input's chunk is the short text itself.
    assert flat[0] == short
    # Last input's chunk is the other short text.
    assert flat[-1] == "another short"


async def test_mixed_batch_short_input_vector_matches_unwrapped() -> None:
    """Short inputs (single chunk) should produce the same vector that the
    underlying embedder would produce for that exact text. Max-pool of one
    vector is that vector."""
    inner = FakeEmbedder()
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy, sparse_window_tokens=64, sparse_overlap_tokens=8)

    short = "trust but verify"
    [wrapped_out] = await embedder.embed_sparse([short])
    [direct_out] = await inner.embed_sparse([short])
    assert wrapped_out == direct_out


# ---------------------------------------------------------------------------
# 5. Dense + rerank pass-through
# ---------------------------------------------------------------------------


async def test_dense_passes_through_unchanged() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy)

    long_text = _long_english_text(approx_tokens=600)
    out = await embedder.embed_dense([long_text, "short"])

    assert len(out) == 2
    # No chunking applied to dense; one call with both inputs as given.
    assert len(spy.embed_dense_calls) == 1
    assert spy.embed_dense_calls[0] == [long_text, "short"]


async def test_rerank_passes_through_unchanged() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy)

    long_text = _long_english_text(approx_tokens=600)
    scores = await embedder.rerank("query", [long_text, "candidate"])

    assert len(scores) == 2
    assert len(spy.rerank_calls) == 1
    assert spy.rerank_calls[0] == ("query", [long_text, "candidate"])


# ---------------------------------------------------------------------------
# 6. Empty input
# ---------------------------------------------------------------------------


async def test_empty_sparse_input_returns_empty_and_skips_downstream() -> None:
    spy = _SpyEmbedder()
    embedder = ChunkedEmbedder(spy)

    out = await embedder.embed_sparse([])

    assert out == []
    assert spy.embed_sparse_calls == [], "no downstream call for empty input"


# ---------------------------------------------------------------------------
# 7. Protocol conformance
# ---------------------------------------------------------------------------


def test_chunked_embedder_implements_embedder_protocol() -> None:
    embedder = ChunkedEmbedder(FakeEmbedder())
    assert isinstance(embedder, Embedder)
