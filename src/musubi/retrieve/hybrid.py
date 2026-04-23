"""Hybrid dense+sparse retrieval using Qdrant server-side RRF fusion."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError
from qdrant_client import QdrantClient, models

from musubi.config import get_settings
from musubi.embedding.base import Embedder
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME, collection_has_sparse
from musubi.types.common import Err, LifecycleState, Namespace, Ok, Result

HYBRID_PREFETCH_LIMIT = 50
_DEFAULT_VISIBLE_STATES: tuple[LifecycleState, ...] = ("matured", "promoted")


@dataclass(frozen=True, slots=True)
class HybridHit:
    """One typed search hit returned by hybrid retrieval."""

    object_id: str
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetrievalError:
    """Typed retrieval error carried in ``Err`` results."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class _QueryEmbedding:
    dense: list[float]
    sparse: dict[int, float]


class QueryEmbeddingCache:
    """Small in-process LRU cache for query embeddings."""

    def __init__(self, *, model_version: str, maxsize: int = 10_000) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._model_version = model_version
        self._maxsize = maxsize
        self._items: OrderedDict[str, _QueryEmbedding] = OrderedDict()

    @property
    def model_version(self) -> str:
        return self._model_version

    def set_model_version(self, model_version: str) -> None:
        if model_version != self._model_version:
            self._model_version = model_version
            self.clear()

    def clear(self) -> None:
        self._items.clear()

    def get(self, query: str) -> _QueryEmbedding | None:
        cached = self._items.get(query)
        if cached is None:
            return None
        self._items.move_to_end(query)
        return _QueryEmbedding(dense=list(cached.dense), sparse=dict(cached.sparse))

    def put(self, query: str, embedding: _QueryEmbedding) -> None:
        self._items[query] = _QueryEmbedding(
            dense=list(embedding.dense),
            sparse=dict(embedding.sparse),
        )
        self._items.move_to_end(query)
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)


async def hybrid_search(
    client: QdrantClient,
    embedder: Embedder,
    *,
    namespace: Namespace,
    query: str,
    collection: str,
    limit: int = 10,
    state_filter: Sequence[LifecycleState] | None = None,
    dense_weight: float = 1.0,
    sparse_weight: float = 1.0,
    include_archived: bool = False,
    prefetch_limit: int | None = None,
    cache: QueryEmbeddingCache | None = None,
    timeout_s: float | None = None,
    sparse_timeout_s: float | None = None,
) -> Result[list[HybridHit], RetrievalError]:
    """Run one hybrid query against one Qdrant collection."""

    if not query:
        return Err(error=RetrievalError(code="empty_query", detail="query must not be empty"))
    if limit <= 0:
        return Err(error=RetrievalError(code="invalid_limit", detail="limit must be positive"))
    if dense_weight <= 0.0 and sparse_weight <= 0.0:
        return Err(
            error=RetrievalError(
                code="invalid_weights",
                detail="at least one retrieval channel must have a positive weight",
            )
        )

    # Dense-only collections (e.g. musubi_artifact) don't declare a sparse
    # vector channel; querying sparse_splade_v1 against them makes Qdrant
    # reject the request with 400 "Not existing vector name" (see #208).
    dense_enabled = dense_weight > 0.0
    sparse_enabled = sparse_weight > 0.0 and collection_has_sparse(collection)

    encoding = await _encode_query(
        embedder,
        query=query,
        cache=cache,
        dense_enabled=dense_enabled,
        sparse_enabled=sparse_enabled,
        sparse_timeout_s=sparse_timeout_s,
    )
    if isinstance(encoding, Err):
        return encoding

    resolved_prefetch_limit = (
        prefetch_limit if prefetch_limit is not None else _prefetch_limit_from_settings()
    )
    query_filter = _build_filter(
        namespace=namespace,
        state_filter=state_filter,
        include_archived=include_archived,
    )
    prefetch = _build_prefetch(
        encoding.value,
        limit=resolved_prefetch_limit,
        dense_enabled=dense_enabled,
        sparse_enabled=sparse_enabled,
    )
    if not prefetch:
        return Err(
            error=RetrievalError(
                code="no_query_vectors",
                detail="no query vectors were available for retrieval",
            )
        )

    try:
        response = await _query_points(
            client,
            collection=collection,
            prefetch=prefetch,
            query_filter=query_filter,
            limit=limit,
            timeout_s=timeout_s,
        )
    except TimeoutError:
        return Ok(value=[])
    except Exception as exc:
        return Err(
            error=RetrievalError(
                code="qdrant_query_failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    return Ok(value=_hits_from_response(response))


async def hybrid_search_many(
    clients: Sequence[QdrantClient],
    embedder: Embedder,
    *,
    namespace: Namespace,
    query: str,
    collections: Sequence[str],
    limit: int = 10,
    state_filter: Sequence[LifecycleState] | None = None,
    dense_weight: float = 1.0,
    sparse_weight: float = 1.0,
    include_archived: bool = False,
    prefetch_limit: int | None = None,
    cache: QueryEmbeddingCache | None = None,
    timeout_s: float | None = None,
    sparse_timeout_s: float | None = None,
) -> Result[list[HybridHit], RetrievalError]:
    """Fan out hybrid search across collections in parallel, then dedupe by object id."""

    if len(clients) != len(collections):
        return Err(
            error=RetrievalError(
                code="fanout_mismatch",
                detail="clients and collections must have the same length",
            )
        )

    results = await asyncio.gather(
        *(
            hybrid_search(
                client,
                embedder,
                namespace=namespace,
                query=query,
                collection=collection,
                limit=limit,
                state_filter=state_filter,
                dense_weight=dense_weight,
                sparse_weight=sparse_weight,
                include_archived=include_archived,
                prefetch_limit=prefetch_limit,
                cache=cache,
                timeout_s=timeout_s,
                sparse_timeout_s=sparse_timeout_s,
            )
            for client, collection in zip(clients, collections, strict=True)
        )
    )

    errors = [result.error for result in results if isinstance(result, Err)]
    if errors:
        return Err(error=errors[0])

    merged: dict[str, HybridHit] = {}
    for result in results:
        for hit in cast(Ok[list[HybridHit]], result).value:
            previous = merged.get(hit.object_id)
            if previous is None or hit.score > previous.score:
                merged[hit.object_id] = hit

    hits = sorted(merged.values(), key=lambda hit: (-hit.score, hit.object_id))
    return Ok(value=hits[:limit])


def _prefetch_limit_from_settings() -> int:
    try:
        settings = get_settings()
    except ValidationError:
        return HYBRID_PREFETCH_LIMIT
    configured = getattr(settings, "hybrid_prefetch_limit", HYBRID_PREFETCH_LIMIT)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return HYBRID_PREFETCH_LIMIT
    return value if value > 0 else HYBRID_PREFETCH_LIMIT


async def _encode_query(
    embedder: Embedder,
    *,
    query: str,
    cache: QueryEmbeddingCache | None,
    dense_enabled: bool,
    sparse_enabled: bool,
    sparse_timeout_s: float | None,
) -> Result[_QueryEmbedding, RetrievalError]:
    cached = cache.get(query) if cache is not None and dense_enabled and sparse_enabled else None
    if cached is not None:
        return Ok(value=cached)

    dense_task: asyncio.Task[list[list[float]]] | None = None
    sparse_task: asyncio.Task[list[dict[int, float]]] | None = None

    if dense_enabled:
        dense_task = asyncio.create_task(embedder.embed_dense([query]))
    if sparse_enabled:
        sparse = embedder.embed_sparse([query])
        if sparse_timeout_s is not None:
            sparse = asyncio.wait_for(sparse, timeout=sparse_timeout_s)
        sparse_task = asyncio.create_task(sparse)

    dense_vector: list[float] = []
    sparse_vector: dict[int, float] = {}

    if dense_task is not None:
        try:
            dense_vector = (await dense_task)[0]
        except Exception as exc:
            return Err(
                error=RetrievalError(
                    code="dense_embedding_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    if sparse_task is not None:
        try:
            sparse_vector = (await sparse_task)[0]
        except TimeoutError:
            sparse_vector = {}
        except Exception as exc:
            return Err(
                error=RetrievalError(
                    code="sparse_embedding_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    embedding = _QueryEmbedding(dense=dense_vector, sparse=sparse_vector)
    if cache is not None and dense_vector and sparse_vector:
        cache.put(query, embedding)
    return Ok(value=embedding)


def _build_prefetch(
    embedding: _QueryEmbedding,
    *,
    limit: int,
    dense_enabled: bool,
    sparse_enabled: bool,
) -> list[models.Prefetch]:
    prefetch: list[models.Prefetch] = []
    if dense_enabled and embedding.dense:
        prefetch.append(
            models.Prefetch(
                query=embedding.dense,
                using=DENSE_VECTOR_NAME,
                limit=limit,
            )
        )
    if sparse_enabled and embedding.sparse:
        prefetch.append(
            models.Prefetch(
                query=models.SparseVector(
                    indices=list(embedding.sparse.keys()),
                    values=list(embedding.sparse.values()),
                ),
                using=SPARSE_VECTOR_NAME,
                limit=limit,
            )
        )
    return prefetch


def _build_filter(
    *,
    namespace: Namespace,
    state_filter: Sequence[LifecycleState] | None,
    include_archived: bool,
) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))
    ]
    states = state_filter
    if states is None and not include_archived:
        states = _DEFAULT_VISIBLE_STATES
    if states is not None:
        must.append(
            models.FieldCondition(
                key="state",
                match=models.MatchAny(any=[str(state) for state in states]),
            )
        )
    return models.Filter(must=must)


async def _query_points(
    client: QdrantClient,
    *,
    collection: str,
    prefetch: list[models.Prefetch],
    query_filter: models.Filter,
    limit: int,
    timeout_s: float | None,
) -> Any:
    async def run() -> Any:
        return await asyncio.to_thread(
            client.query_points,
            collection_name=collection,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

    if timeout_s is None:
        return await run()
    return await asyncio.wait_for(run(), timeout=timeout_s)


def _hits_from_response(response: Any) -> list[HybridHit]:
    hits: list[HybridHit] = []
    seen: set[str] = set()
    for point in response.points:
        payload = dict(point.payload or {})
        object_id = str(payload.get("object_id", point.id))
        if object_id in seen:
            continue
        seen.add(object_id)
        hits.append(
            HybridHit(
                object_id=object_id,
                score=float(point.score),
                payload=payload,
            )
        )
    return hits


__all__ = [
    "HYBRID_PREFETCH_LIMIT",
    "HybridHit",
    "QueryEmbeddingCache",
    "RetrievalError",
    "hybrid_search",
    "hybrid_search_many",
]
