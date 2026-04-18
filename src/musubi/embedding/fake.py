"""Deterministic in-process embedder for tests.

Seed strategy: SHA-256 of each input text feeds a ``random.Random``. Dense
outputs are L2-normalised so cosine distance is meaningful and unit-norm
invariants hold. Sparse outputs deterministically pick a handful of term
indexes with weights in ``(0, 1]``. Rerank scores are a deterministic
function of ``(query, candidate)`` pairs, again in ``[0, 1]``.

Properties guaranteed:

- ``embed_dense`` vectors have exactly ``dense_size`` dimensions and unit
  L2 norm.
- Output is deterministic — the same input always yields the same vector.
- Input order is preserved in the output list.
"""

from __future__ import annotations

import hashlib
import math
import random

from musubi.embedding.base import Embedder
from musubi.store.specs import DENSE_SIZE


def _seed_for(text: str, *, salt: str) -> int:
    """Stable integer seed derived from ``text`` and a per-method salt."""
    digest = hashlib.sha256(f"{salt}:{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _dense_vector(text: str, size: int) -> list[float]:
    rng = random.Random(_seed_for(text, salt="dense"))
    raw = [rng.gauss(0.0, 1.0) for _ in range(size)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _sparse_vector(text: str, *, vocab: int = 30_000, k: int = 16) -> dict[int, float]:
    rng = random.Random(_seed_for(text, salt="sparse"))
    out: dict[int, float] = {}
    while len(out) < k:
        idx = rng.randint(0, vocab - 1)
        weight = rng.random()
        if weight > 0.0:
            out[idx] = weight
    return out


def _rerank_score(query: str, candidate: str) -> float:
    rng = random.Random(_seed_for(f"{query}\x00{candidate}", salt="rerank"))
    # Uniform in [0, 1]; deterministic per (query, candidate) pair.
    return rng.random()


class FakeEmbedder(Embedder):
    """In-process stand-in for a real :class:`Embedder`.

    Safe to instantiate anywhere — no network, no GPU. Useful in unit tests
    that need a reliable Embedder but don't care about quality.
    """

    def __init__(self, *, dense_size: int = DENSE_SIZE) -> None:
        if dense_size <= 0:
            raise ValueError("dense_size must be positive")
        self._dense_size = dense_size

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return [_dense_vector(t, self._dense_size) for t in texts]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return [_sparse_vector(t) for t in texts]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [_rerank_score(query, c) for c in candidates]


__all__ = ["FakeEmbedder"]
