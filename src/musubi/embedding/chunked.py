"""Length-aware Embedder wrapper.

SPLADE-v3 has a hard 512-token input cap (``max_position_embeddings`` baked
into the model). The existing :class:`Embedder` protocol abstracts the model
behind a "give me an embedding for this text" interface, but in practice the
limit leaks up to callers — long inputs hit the encoder and return HTTP 413.

:class:`ChunkedEmbedder` closes that leak. It wraps any :class:`Embedder` and
honors the sparse encoder's contract by:

1. tokenizing each input with SPLADE-v3's own tokenizer (loaded once at
   process scope via :mod:`musubi.planes.artifact.chunking`), so the token
   count is exact against the encoder that will see the chunks;
2. routing inputs that exceed a 510-token window (512 cap minus the two
   special tokens SPLADE prepends/appends) through a sliding-window chunker;
3. embedding all chunks (batched across inputs in a single downstream call);
4. **max-pooling** the per-chunk sparse vectors into one aggregate per input
   — the canonical aggregation for SPLADE-style term-presence vectors.

Dense + rerank pass through unchanged. BGE-M3 supports 8192 tokens natively,
and rerank inputs are short by convention; neither needs chunking today. If
the deployed TEI dense server is ever configured to cap dense at 512, dense
gets the same treatment with mean-pooling (not implemented here; add when
the constraint is real).

Why max-pool for sparse: SPLADE vectors are sparse over vocabulary; each
non-zero entry asserts "this term has weight W in the document." Max across
chunks preserves "term X appears in this document with weight ≥W," matching
the underlying retrieval semantic. Averaging dilutes single-chunk terms;
summing inflates. Max is the SPLADE long-doc standard.
"""

from __future__ import annotations

from musubi.embedding.base import Embedder
from musubi.planes.artifact.chunking import (
    TokenizerProtocol,
    TokenSlidingChunker,
    load_splade_v3_tokenizer,
)

# SPLADE-v3's max_position_embeddings is 512 and the model wraps each input
# with two special tokens (``[CLS]`` … ``[SEP]``). 510 is the largest content
# window that guarantees the encoder will accept it.
_DEFAULT_SPARSE_WINDOW_TOKENS = 510
_DEFAULT_SPARSE_OVERLAP_TOKENS = 64


def _max_pool_sparse(vectors: list[dict[int, float]]) -> dict[int, float]:
    """Per-vocabulary-index max across a list of sparse vectors.

    Empty input yields an empty vector. Single-vector input yields a copy
    of that vector (no aliasing into caller state).
    """
    if not vectors:
        return {}
    if len(vectors) == 1:
        return dict(vectors[0])
    pooled: dict[int, float] = {}
    for vec in vectors:
        for idx, weight in vec.items():
            current = pooled.get(idx)
            if current is None or weight > current:
                pooled[idx] = weight
    return pooled


class ChunkedEmbedder(Embedder):
    """Wrap an :class:`Embedder` so long inputs are sparse-chunked + pooled.

    Dense and rerank delegate unchanged. ``embed_sparse`` always tokenizes
    each input via the sliding-window chunker — inputs that fit in one window
    take the single-chunk path (one downstream embedding, no pooling); inputs
    that don't are split into overlapping windows and max-pooled into a single
    output vector per input.

    Chunks across all input texts are flattened into one downstream batch so
    we keep the round-trip count to one per :meth:`embed_sparse` call regardless
    of how many inputs were long.
    """

    def __init__(
        self,
        wrapped: Embedder,
        *,
        tokenizer: TokenizerProtocol | None = None,
        sparse_window_tokens: int = _DEFAULT_SPARSE_WINDOW_TOKENS,
        sparse_overlap_tokens: int = _DEFAULT_SPARSE_OVERLAP_TOKENS,
    ) -> None:
        """Wrap an :class:`Embedder` with length-aware sparse chunking.

        ``tokenizer`` is optional — by default the SPLADE-v3 tokenizer is
        lazy-loaded on the first :meth:`embed_sparse` call so construction
        stays hermetic (tests can build a ``ChunkedEmbedder`` without an
        HF download, an HF token, or any network access). Tests that want
        deterministic chunking inject a fake :class:`TokenizerProtocol`.
        """
        self._wrapped = wrapped
        self._tokenizer = tokenizer
        self._sparse_window_tokens = sparse_window_tokens
        self._sparse_overlap_tokens = sparse_overlap_tokens
        self._chunker: TokenSlidingChunker | None = None

    def _get_chunker(self) -> TokenSlidingChunker:
        """Lazy-build the chunker on first sparse call.

        Caches the constructed chunker so subsequent calls skip the
        tokenizer-load + chunker-construct cost.
        """
        if self._chunker is None:
            tokenizer = self._tokenizer or load_splade_v3_tokenizer()
            self._chunker = TokenSlidingChunker(
                tokenizer=tokenizer,
                window_tokens=self._sparse_window_tokens,
                overlap_tokens=self._sparse_overlap_tokens,
            )
        return self._chunker

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await self._wrapped.embed_dense(texts)

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        if not texts:
            return []

        chunker = self._get_chunker()
        per_input_chunks: list[list[str]] = []
        for text in texts:
            chunks = chunker.chunk(text)
            # Whitespace-only inputs survive chunking as a single RawChunk
            # whose content is "" (TokenSlidingChunker trims via _trim_span).
            # Forward the original text in that case so cache keys and
            # downstream embedder behavior stay consistent with the pre-wrap
            # contract.
            if not chunks or not any(c.content for c in chunks):
                per_input_chunks.append([text])
            else:
                per_input_chunks.append([c.content for c in chunks])

        flat_inputs: list[str] = []
        spans: list[tuple[int, int]] = []
        cursor = 0
        for sublist in per_input_chunks:
            spans.append((cursor, cursor + len(sublist)))
            flat_inputs.extend(sublist)
            cursor += len(sublist)

        flat_vectors = await self._wrapped.embed_sparse(flat_inputs)

        return [_max_pool_sparse(flat_vectors[start:end]) for start, end in spans]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return await self._wrapped.rerank(query, candidates)


__all__ = ["ChunkedEmbedder"]
