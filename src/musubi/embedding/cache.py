"""Small in-memory cache wrapper for query-time embeddings."""

from __future__ import annotations

import hashlib

from musubi.embedding.base import Embedder


class CachedEmbedder(Embedder):
    """Cache dense and sparse query embeddings by text hash + model revision."""

    def __init__(
        self, wrapped: Embedder, *, model_revision: str, max_entries: int = 10_000
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._wrapped = wrapped
        self._model_revision = model_revision
        self._max_entries = max_entries
        self._dense: dict[str, list[float]] = {}
        self._sparse: dict[str, dict[int, float]] = {}

    def set_model_revision(self, model_revision: str) -> None:
        """Switch revision and clear stale vectors if the model changed."""

        if model_revision == self._model_revision:
            return
        self._model_revision = model_revision
        self.clear()

    def clear(self) -> None:
        self._dense.clear()
        self._sparse.clear()

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        keys = [self._cache_key(text, kind="dense") for text in texts]
        missing_texts = [
            text for text, key in zip(texts, keys, strict=True) if key not in self._dense
        ]
        if missing_texts:
            missing_vectors = await self._wrapped.embed_dense(missing_texts)
            for text, vector in zip(missing_texts, missing_vectors, strict=True):
                self._remember_dense(self._cache_key(text, kind="dense"), vector)
        return [list(self._dense[key]) for key in keys]

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        keys = [self._cache_key(text, kind="sparse") for text in texts]
        missing_texts = [
            text for text, key in zip(texts, keys, strict=True) if key not in self._sparse
        ]
        if missing_texts:
            missing_vectors = await self._wrapped.embed_sparse(missing_texts)
            for text, vector in zip(missing_texts, missing_vectors, strict=True):
                self._remember_sparse(self._cache_key(text, kind="sparse"), vector)
        return [dict(self._sparse[key]) for key in keys]

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return await self._wrapped.rerank(query, candidates)

    def _cache_key(self, text: str, *, kind: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        return f"{self._model_revision}:{kind}:{digest}"

    def _remember_dense(self, key: str, vector: list[float]) -> None:
        if len(self._dense) >= self._max_entries:
            self._dense.pop(next(iter(self._dense)))
        self._dense[key] = list(vector)

    def _remember_sparse(self, key: str, vector: dict[int, float]) -> None:
        if len(self._sparse) >= self._max_entries:
            self._sparse.pop(next(iter(self._sparse)))
        self._sparse[key] = dict(vector)


__all__ = ["CachedEmbedder"]
