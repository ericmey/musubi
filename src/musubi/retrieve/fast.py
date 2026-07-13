"""Latency-budgeted retrieval path for interactive callers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.retrieve.hybrid import (
    HybridHit,
    HybridSearchResult,
    QueryEmbeddingCache,
    RetrievalError,
    hybrid_search,
)
from musubi.retrieve.scoring import SCORE_WEIGHTS, Hit, ScoreComponents, ScoreWeights, score
from musubi.retrieve.warnings import RetrievalWarning, plane_error, plane_timeout
from musubi.types.common import Err, LifecycleState, Namespace, Ok, Result

_DEFAULT_STATES: tuple[LifecycleState, ...] = ("matured", "promoted")
_DEFAULT_CACHE_MODEL_VERSION = "fast-path-v1"
_DEFAULT_RESPONSE_TTL_S = 30.0


@dataclass(frozen=True, slots=True)
class FastRetrievalError:
    """Typed error returned by the fast retrieval boundary."""

    code: str
    detail: str
    status_code: int
    retry_after_s: int | None = None


@dataclass(frozen=True, slots=True)
class FastHit:
    """One packed fast-path retrieval hit."""

    object_id: str
    score: float
    score_components: ScoreComponents
    payload: dict[str, Any]
    snippet: str
    lineage_summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FastRetrieveResult:
    """Successful fast retrieval response."""

    results: list[FastHit]
    warnings: tuple[RetrievalWarning, ...]
    status_code: int = 200
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    inserted_at: float
    result: FastRetrieveResult


class FastResponseCache:
    """Tiny exact-query TTL cache for repeated interactive calls."""

    def __init__(self, *, ttl_s: float = _DEFAULT_RESPONSE_TTL_S) -> None:
        if ttl_s <= 0.0:
            raise ValueError("ttl_s must be positive")
        self._ttl_s = ttl_s
        self._items: dict[tuple[Any, ...], _CachedResponse] = {}

    def get(self, key: tuple[Any, ...], *, now: float) -> FastRetrieveResult | None:
        cached = self._items.get(key)
        if cached is None:
            return None
        if now - cached.inserted_at > self._ttl_s:
            del self._items[key]
            return None
        return replace(cached.result, cache_hit=True)

    def put(self, key: tuple[Any, ...], result: FastRetrieveResult, *, now: float) -> None:
        self._items[key] = _CachedResponse(
            inserted_at=now,
            result=replace(result, cache_hit=False),
        )


async def run_fast_retrieve(
    client: QdrantClient | Sequence[QdrantClient],
    embedder: Embedder,
    *,
    namespace: Namespace,
    query: str,
    collection: str | None = None,
    collections: Sequence[str] | None = None,
    limit: int = 5,
    now: float | None = None,
    state_filter: Sequence[LifecycleState] = _DEFAULT_STATES,
    prefetch_limit: int | None = None,
    plane_timeout_s: float = 0.250,
    sparse_timeout_s: float | None = None,
    embedding_cache: QueryEmbeddingCache | None = None,
    response_cache: FastResponseCache | None = None,
    weights: ScoreWeights = SCORE_WEIGHTS,
) -> Result[FastRetrieveResult, FastRetrievalError]:
    """Run the interactive retrieval path without expensive downstream steps."""

    if not query:
        return Err(
            error=FastRetrievalError(
                code="empty_query",
                detail="query must not be empty",
                status_code=400,
            )
        )
    if limit <= 0:
        return Err(
            error=FastRetrievalError(
                code="invalid_limit",
                detail="limit must be positive",
                status_code=400,
            )
        )

    timestamp = time.time() if now is None else now
    resolved_collections = _resolve_collections(collection=collection, collections=collections)
    if isinstance(resolved_collections, Err):
        return resolved_collections
    resolved_clients = _resolve_clients(client, count=len(resolved_collections.value))
    if isinstance(resolved_clients, Err):
        return resolved_clients

    cache_key = _cache_key(
        namespace=namespace,
        query=query,
        collections=resolved_collections.value,
        limit=limit,
        state_filter=state_filter,
    )
    if response_cache is not None:
        cached = response_cache.get(cache_key, now=timestamp)
        if cached is not None:
            return Ok(value=cached)

    cache = embedding_cache or QueryEmbeddingCache(model_version=_DEFAULT_CACHE_MODEL_VERSION)
    prefetch = prefetch_limit if prefetch_limit is not None else max(20, limit * 2)
    plane_results = await asyncio.gather(
        *(
            _query_one(
                plane_client,
                embedder,
                namespace=namespace,
                query=query,
                collection=plane_collection,
                limit=prefetch,
                state_filter=state_filter,
                cache=cache,
                plane_timeout_s=plane_timeout_s,
                sparse_timeout_s=sparse_timeout_s,
            )
            for plane_client, plane_collection in zip(
                resolved_clients.value, resolved_collections.value, strict=True
            )
        )
    )

    hits: list[HybridHit] = []
    warnings: list[RetrievalWarning] = []
    errors: list[RetrievalError] = []
    timeouts = 0
    contributors = 0
    for collection_name, plane_result in plane_results:
        # RET-007: one bounded warning language — a timed-out plane is plane_timeout_<plane>, any other
        # leg failure is plane_error_<plane>, and a surviving leg threads up its own sparse warnings.
        plane = collection_name.removeprefix("musubi_")
        if isinstance(plane_result, Ok):
            contributors += 1
            hits.extend(plane_result.value.hits)
            warnings.extend(plane_result.value.warnings)
        elif "timeout" in plane_result.error.code:
            warnings.append(plane_timeout(plane))
            timeouts += 1
        else:
            errors.append(plane_result.error)
            warnings.append(plane_error(plane))

    deduped = _dedupe(hits)
    # RET-007 Blocker 1: an all-plane total failure is an Err, NOT Ok(empty). A non-timeout leg error
    # maps through _map_error; an all-timeout-no-survivor case is a bounded timeout (→ 503).
    if not deduped and errors:
        return Err(error=_map_error(errors[0]))
    if not deduped and contributors == 0 and timeouts == len(plane_results):
        return Err(
            error=FastRetrievalError(
                code="all_planes_timeout",
                detail="all planes timed out",
                status_code=503,
                retry_after_s=5,
            )
        )

    result = FastRetrieveResult(
        results=_pack(deduped, now=timestamp, limit=limit, weights=weights),
        warnings=tuple(warnings),
    )
    if response_cache is not None:
        response_cache.put(cache_key, result, now=timestamp)
    return Ok(value=result)


async def _query_one(
    client: QdrantClient,
    embedder: Embedder,
    *,
    namespace: Namespace,
    query: str,
    collection: str,
    limit: int,
    state_filter: Sequence[LifecycleState],
    cache: QueryEmbeddingCache,
    plane_timeout_s: float,
    sparse_timeout_s: float | None,
) -> tuple[str, Result[HybridSearchResult, RetrievalError]]:
    try:
        result = await asyncio.wait_for(
            hybrid_search(
                client,
                embedder,
                namespace=namespace,
                query=query,
                collection=collection,
                limit=limit,
                state_filter=state_filter,
                cache=cache,
                timeout_s=plane_timeout_s,
                sparse_timeout_s=sparse_timeout_s,
            ),
            timeout=plane_timeout_s,
        )
    except TimeoutError:
        result = Err(error=RetrievalError(code="plane_timeout", detail="plane timed out"))
    return collection, result


def _resolve_collections(
    *,
    collection: str | None,
    collections: Sequence[str] | None,
) -> Result[tuple[str, ...], FastRetrievalError]:
    if collections is not None:
        if not collections:
            return Err(
                error=FastRetrievalError(
                    code="invalid_collections",
                    detail="collections must not be empty",
                    status_code=400,
                )
            )
        return Ok(value=tuple(collections))
    if collection is not None:
        return Ok(value=(collection,))
    return Err(
        error=FastRetrievalError(
            code="invalid_collections",
            detail="collection or collections is required",
            status_code=400,
        )
    )


def _resolve_clients(
    client: QdrantClient | Sequence[QdrantClient],
    *,
    count: int,
) -> Result[tuple[QdrantClient, ...], FastRetrievalError]:
    if isinstance(client, Sequence):
        clients = tuple(client)
        if len(clients) != count:
            return Err(
                error=FastRetrievalError(
                    code="fanout_mismatch",
                    detail="clients and collections must have the same length",
                    status_code=400,
                )
            )
        return Ok(value=clients)
    return Ok(value=tuple(client for _ in range(count)))


def _map_error(error: RetrievalError) -> FastRetrievalError:
    if error.code in {"no_query_vectors", "dense_embedding_failed", "sparse_embedding_failed"}:
        return FastRetrievalError(
            code="embeddings_unavailable",
            detail=error.detail,
            status_code=503,
            retry_after_s=5,
        )
    if error.code == "qdrant_query_failed":
        return FastRetrievalError(
            code="index_unavailable",
            detail=error.detail,
            status_code=503,
        )
    return FastRetrievalError(code=error.code, detail=error.detail, status_code=503)


def _dedupe(hits: Sequence[HybridHit]) -> list[HybridHit]:
    best_by_id: dict[str, HybridHit] = {}
    for hit in hits:
        previous = best_by_id.get(hit.object_id)
        if previous is None or hit.score > previous.score:
            best_by_id[hit.object_id] = hit
    return list(best_by_id.values())


def _pack(
    hits: Sequence[HybridHit],
    *,
    now: float,
    limit: int,
    weights: ScoreWeights,
) -> list[FastHit]:
    batch_max = max((hit.score for hit in hits), default=1.0)
    packed: list[FastHit] = []
    for hybrid_hit in hits:
        payload = dict(hybrid_hit.payload)
        total, components = score(
            _to_score_hit(hybrid_hit, payload=payload, batch_max=batch_max, now=now),
            now=now,
            weights=weights,
        )
        packed.append(
            FastHit(
                object_id=hybrid_hit.object_id,
                score=total,
                score_components=components,
                payload=payload,
                snippet=_snippet(payload),
                lineage_summary=_lineage_summary(payload),
            )
        )
    return sorted(packed, key=lambda hit: (-hit.score, hit.object_id))[:limit]


def _to_score_hit(
    hit: HybridHit,
    *,
    payload: dict[str, Any],
    batch_max: float,
    now: float,
) -> Hit:
    return Hit(
        object_id=hit.object_id,
        plane=str(payload.get("plane", _plane_from_namespace(payload))),
        state=str(payload.get("state", "matured")),
        rrf_score=hit.score,
        batch_max_rrf=batch_max,
        updated_epoch=float(payload.get("updated_epoch", now)),
        importance=int(payload.get("importance", 5)),
        reinforcement_count=int(payload.get("reinforcement_count", 0)),
        access_count=int(payload.get("access_count", 0)),
        payload=payload,
    )


def _plane_from_namespace(payload: dict[str, Any]) -> str:
    namespace = str(payload.get("namespace", ""))
    if "/" in namespace:
        return namespace.rsplit("/", maxsplit=1)[-1]
    return "episodic"


def _snippet(payload: dict[str, Any]) -> str:
    content = str(payload.get("content") or payload.get("title") or "")
    return content[:200]


def _lineage_summary(payload: dict[str, Any]) -> dict[str, Any]:
    lineage = payload.get("lineage")
    summary = dict(lineage) if isinstance(lineage, dict) else {}
    for key in ("promoted_to", "promoted_from", "supersedes", "superseded_by"):
        value = payload.get(key)
        if value is not None:
            summary[key] = value
    return {key: value for key, value in summary.items() if value is not None}


def _cache_key(
    *,
    namespace: Namespace,
    query: str,
    collections: Sequence[str],
    limit: int,
    state_filter: Sequence[LifecycleState],
) -> tuple[Any, ...]:
    """Cache key scoped to identity family, not exact namespace.

    Retrieval federates at the identity level (see
    ``musubi.retrieve.hybrid._build_filter``), so two queries from
    different presences of the same identity — e.g. caller namespaces
    ``aoi/command-chair/episodic`` vs ``aoi/voice/episodic`` — produce
    identical results and SHOULD share a cache entry. Keying on the
    full namespace would split that cache and miss on every cross-
    substrate query.
    """
    from musubi.types.common import family_of

    return (family_of(namespace), query, tuple(collections), limit, tuple(state_filter))


__all__ = [
    "FastHit",
    "FastResponseCache",
    "FastRetrievalError",
    "FastRetrieveResult",
    "run_fast_retrieve",
]
