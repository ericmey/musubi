"""Embedder protocol + typed error.

The protocol is ``runtime_checkable`` so ``isinstance(fake, Embedder)`` in
tests is meaningful. All methods are async; concrete implementations run
either against a real TEI endpoint (via httpx) or in-process (the fake).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """Raised by the TEI clients when a request fails.

    Wraps the underlying ``httpx.HTTPError`` or a 4xx/5xx response. The
    ``status_code`` is ``None`` for network-level failures (connect, timeout,
    DNS) so callers can distinguish "service said no" from "couldn't reach
    service".
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class Embedder(Protocol):
    """The complete embedding surface exposed to the rest of Musubi.

    Implementations:

    - :class:`musubi.embedding.fake.FakeEmbedder` — deterministic unit-test
      double; does not talk to the network.
    - A composite adapter that delegates dense, sparse, and rerank to the
      three :mod:`musubi.embedding.tei` clients respectively (not in this
      slice; callers compose per need today).

    All methods preserve input order: the ``i``-th output corresponds to the
    ``i``-th input.
    """

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input."""
        ...

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """Return one ``index -> weight`` sparse vector per input."""
        ...

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Return one relevance score per candidate (candidate order)."""
        ...


__all__ = ["Embedder", "EmbeddingError"]
