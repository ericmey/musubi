"""Deep-path retrieval orchestration.

Full hybrid + cross-encoder rerank + lineage hydration. Milliseconds-to-seconds budget.
Implements [[05-retrieval/deep-path]].
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from pydantic import ValidationError
from qdrant_client import QdrantClient

from musubi.config import get_settings
from musubi.embedding.base import Embedder
from musubi.embedding.tei import TEIRerankerClient
from musubi.planes.artifact.plane import ArtifactPlane
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.hybrid import HybridHit, HybridSearchResult, hybrid_search
from musubi.retrieve.rerank import RerankResult, hybrid_fallback, rerank
from musubi.retrieve.scoring import Hit, ScoredHit, rank_hits
from musubi.retrieve.warnings import RetrievalWarning, reranker_failed
from musubi.settings import Settings
from musubi.store.names import collection_for_plane
from musubi.types.common import Err, LifecycleState, Ok, Result, utc_now

logger = logging.getLogger(__name__)

_DEFAULT_RERANK_TIMEOUT_S = float(Settings.model_fields["retrieval_rerank_timeout_s"].default)
_DEFAULT_LINEAGE_HYDRATE_TIMEOUT_S = float(
    Settings.model_fields["retrieval_lineage_timeout_s"].default
)


@dataclass(frozen=True)
class DeepRetrievalError:
    code: str
    detail: str


@dataclass(frozen=True)
class DeepResult:
    """The success value of :func:`run_deep_retrieve`: ranked ``hits`` plus any RET-007 degradation
    ``warnings`` threaded up from the hybrid legs (e.g. ``sparse_embedding_failed``)."""

    hits: list[ScoredHit]
    warnings: tuple[RetrievalWarning, ...] = ()


@dataclass(frozen=True)
class RetrievalQuery:
    namespace: str
    query_text: str
    mode: str = "deep"
    limit: int = 25
    planes: Sequence[str] = ("curated", "concept", "episodic")
    include_lineage: bool = True
    state_filter: Sequence[LifecycleState] | None = None


class DeepRetrievalLLM(Protocol):
    async def expand_query(self, query: str) -> str | None: ...


class _NotConfiguredDeepLLM:
    async def expand_query(self, query: str) -> str | None:
        return None


async def run_deep_retrieve(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient,
    query: RetrievalQuery,
    llm: DeepRetrievalLLM | None = None,
) -> Result[DeepResult, DeepRetrievalError]:
    """Execute deep-path retrieval.

    Orchestrates hybrid_search -> rerank -> LLM expansion -> score -> lineage.
    """
    if llm is None:
        llm = _NotConfiguredDeepLLM()

    rerank_timeout_s, lineage_timeout_s = _retrieval_stage_timeouts()

    # 1. LLM Query Expansion
    expanded_query = query.query_text
    if not isinstance(llm, _NotConfiguredDeepLLM):
        try:
            expansion = await asyncio.wait_for(llm.expand_query(query.query_text), timeout=2.0)
            if expansion:
                expanded_query = f"{query.query_text}\n\n{expansion}"
        except Exception as e:
            logger.warning("LLM query expansion failed, falling back: %s", e)

    # 2. Hybrid Search
    parts = query.namespace.split("/")
    base_ns = "/".join(parts[:2]) if len(parts) >= 3 else query.namespace

    hybrid_coros = []
    for p in query.planes:
        hybrid_coros.append(
            hybrid_search(
                client=client,
                embedder=embedder,
                namespace=f"{base_ns}/{p}",
                query=expanded_query,
                collection=collection_for_plane(p),
                limit=query.limit * 2,  # pre-fetch more for reranker
                state_filter=query.state_filter,
                # Spec: deep per-plane hybrid budget is 1500ms
                # ([[05-retrieval/orchestration]] / [[05-retrieval/hybrid-search]]).
                timeout_s=1.5,
                sparse_timeout_s=1.0,
            )
        )

    results = await asyncio.gather(*hybrid_coros)

    errors = [res.error for res in results if isinstance(res, Err)]
    if errors:
        return Err(error=DeepRetrievalError(code=errors[0].code, detail=errors[0].detail))

    # Merge and dedup hits; thread each leg's RET-007 degradation warnings up (e.g. a sparse-embedding
    # timeout that fell back to dense-only surfaces sparse_embedding_failed).
    merged: dict[str, HybridHit] = {}
    warnings: list[RetrievalWarning] = []
    for res in results:
        ok = cast(Ok[HybridSearchResult], res)
        warnings.extend(ok.value.warnings)
        for hit in ok.value.hits:
            previous = merged.get(hit.object_id)
            if previous is None or hit.score > previous.score:
                merged[hit.object_id] = hit

    hybrid_hits = list(merged.values())
    if not hybrid_hits:
        return Ok(value=DeepResult(hits=[], warnings=tuple(warnings)))

    # 3. Convert to Hit
    hits: list[Hit] = []
    for h in hybrid_hits:
        ns = str(h.payload.get("namespace", ""))
        plane = ns.split("/")[-1] if "/" in ns else "episodic"

        hits.append(
            Hit(
                object_id=h.object_id,
                plane=plane,
                state=str(h.payload.get("state", "matured")),
                rrf_score=h.score,
                batch_max_rrf=1.0,  # Will be replaced
                updated_epoch=float(h.payload.get("updated_epoch", 0.0)),
                importance=int(h.payload.get("importance", 5)),
                reinforcement_count=int(h.payload.get("reinforcement_count", 0)),
                access_count=int(h.payload.get("access_count", 0)),
                payload=h.payload,
            )
        )

    batch_max = max((h.rrf_score for h in hits), default=1.0)
    if batch_max > 0.0:
        hits = [replace(h, batch_max_rrf=batch_max) for h in hits]

    # 4. Rerank
    # Use the original query text for reranking to preserve strict relevance scoring
    rerank_result = await _rerank_with_timeout(
        client=reranker,
        query_text=query.query_text,
        candidates=hits,
        top_k=query.limit,
        timeout_s=rerank_timeout_s,
    )
    reranked_hits = rerank_result.hits
    warnings.extend(rerank_result.warnings)

    # 5. Score
    now = utc_now().timestamp()
    scored = rank_hits(reranked_hits, now=now)

    # 6. Hydrate Lineage
    if query.include_lineage and scored:
        scored = await _hydrate_lineage_async(
            scored,
            client,
            embedder,
            timeout_s=lineage_timeout_s,
        )

    return Ok(value=DeepResult(hits=scored, warnings=tuple(warnings)))


def _retrieval_stage_timeouts() -> tuple[float, float]:
    """Load tunable stage budgets, retaining deterministic unit-test defaults."""
    try:
        settings = get_settings()
    except ValidationError:
        return _DEFAULT_RERANK_TIMEOUT_S, _DEFAULT_LINEAGE_HYDRATE_TIMEOUT_S
    return settings.retrieval_rerank_timeout_s, settings.retrieval_lineage_timeout_s


async def _rerank_with_timeout(
    client: TEIRerankerClient,
    query_text: str,
    candidates: list[Hit],
    *,
    top_k: int,
    timeout_s: float,
) -> RerankResult:
    """Bound the optional cross-encoder stage and preserve hybrid ordering on timeout."""
    try:
        return await asyncio.wait_for(
            rerank(client, query_text, candidates, top_k=top_k),
            timeout=timeout_s,
        )
    except TimeoutError:
        plane = candidates[0].plane if candidates else "episodic"
        logger.warning(
            "Reranker exceeded stage budget; falling back to hybrid-only. plane=%s timeout_s=%s",
            plane,
            timeout_s,
        )
        return RerankResult(
            hits=hybrid_fallback(candidates, top_k=top_k),
            warnings=(reranker_failed(plane, cause="timeout"),),
        )


def _run_hydrate_one(
    hit: ScoredHit,
    client: QdrantClient,
    embedder: Embedder,
) -> ScoredHit:
    """Run the sync-Qdrant lineage coroutine entirely on a worker thread."""
    return asyncio.run(_hydrate_one(hit, client, embedder))


async def _hydrate_lineage_async(
    hits: list[ScoredHit],
    client: QdrantClient,
    embedder: Embedder,
    *,
    timeout_s: float,
) -> list[ScoredHit]:
    """Hydrate hits concurrently, degrading each timed-out or failed hit in place."""

    async def hydrate_or_original(hit: ScoredHit) -> ScoredHit:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_hydrate_one, hit, client, embedder),
                timeout=timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "Lineage hydrate exceeded stage budget; returning unhydrated hit. object_id=%s "
                "timeout_s=%s",
                hit.object_id,
                timeout_s,
            )
            return hit
        except Exception:
            logger.exception(
                "Lineage hydrate failed; returning unhydrated hit. object_id=%s",
                hit.object_id,
            )
            return hit

    return list(await asyncio.gather(*(hydrate_or_original(hit) for hit in hits)))


async def _hydrate_one(
    hit: ScoredHit,
    client: QdrantClient,
    embedder: Embedder,
) -> ScoredHit:
    """Hydrate lineage references into full objects/snippets."""
    ns = hit.payload.get("namespace", f"unknown/{hit.plane}")
    if not isinstance(ns, str):
        ns = f"unknown/{hit.plane}"

    lineage: dict[str, Any] = {
        "supersedes": [],
        "superseded_by": None,
        "promoted_from": None,
        "promoted_to": None,
        "supported_by": [],
    }

    curated = CuratedPlane(client=client, embedder=embedder)
    concept = ConceptPlane(client=client, embedder=embedder)
    episodic = EpisodicPlane(client=client, embedder=embedder)
    artifact = ArtifactPlane(client=client, embedder=embedder)

    # 1. Fetch base object
    obj: Any = None
    if hit.plane == "curated":
        obj = await asyncio.wait_for(
            curated.get(namespace=ns, object_id=hit.object_id), timeout=1.0
        )
    elif hit.plane == "concept":
        obj = await asyncio.wait_for(
            concept.get(namespace=ns, object_id=hit.object_id), timeout=1.0
        )
    elif hit.plane == "episodic":
        # RET-002: hydration must NOT account. Access is accounted once at the final
        # delivery boundary (orchestration.retrieve), never as a side effect of lineage.
        obj = await asyncio.wait_for(
            episodic.get(namespace=ns, object_id=hit.object_id, bump_access=False), timeout=1.0
        )

    if not obj:
        new_payload = dict(hit.payload)
        new_payload["lineage"] = lineage
        return replace(hit, payload=new_payload)

    new_payload = dict(hit.payload)
    new_payload["content"] = getattr(obj, "content", "")
    if hasattr(obj, "title"):
        new_payload["title"] = obj.title

    # 2. Supersession chain tip
    current = obj
    tip_id = None
    while getattr(current, "superseded_by", None):
        nxt_id = current.superseded_by
        if hit.plane == "curated":
            current = await curated.get(namespace=ns, object_id=nxt_id)
        elif hit.plane == "concept":
            current = await concept.get(namespace=ns, object_id=nxt_id)
        elif hit.plane == "episodic":
            # RET-002: a lineage-walk hop is never a delivered row — never account it.
            current = await episodic.get(namespace=ns, object_id=nxt_id, bump_access=False)
        if not current:
            break
        tip_id = current.object_id

    if tip_id:
        lineage["superseded_by"] = {
            "object_id": tip_id,
            "title": getattr(current, "title", "Untitled"),
            "state": getattr(current, "state", "unknown"),
        }

    # 3. supersedes
    for sid in getattr(obj, "supersedes", []):
        lineage["supersedes"].append(
            {"object_id": sid, "title": "Superseded item", "state": "superseded"}
        )

    # 4. Promoted from/to
    if hit.plane == "curated" and hasattr(obj, "promoted_from") and obj.promoted_from:
        pf = await concept.get(
            namespace=ns.replace("/curated", "/concept"), object_id=obj.promoted_from
        )
        if pf:
            lineage["promoted_from"] = {
                "object_id": pf.object_id,
                "title": getattr(pf, "title", "Untitled"),
            }

    if hit.plane == "concept" and hasattr(obj, "promoted_to") and obj.promoted_to:
        pt = await curated.get(
            namespace=ns.replace("/concept", "/curated"), object_id=obj.promoted_to
        )
        if pt:
            lineage["promoted_to"] = {
                "object_id": pt.object_id,
                "title": getattr(pt, "title", "Untitled"),
            }

    # 5. Supported by
    for art_ref in getattr(obj, "supported_by", []):
        art = await artifact.get(
            namespace=ns.replace(f"/{hit.plane}", "/artifact"), object_id=art_ref.artifact_id
        )
        if art:
            lineage["supported_by"].append(
                {
                    "artifact_id": art.object_id,
                    "chunk_id": art_ref.chunk_id,
                    "title": getattr(art, "name", art.object_id),
                }
            )

    new_payload["lineage"] = lineage
    return replace(hit, payload=new_payload)


__all__ = [
    "DeepResult",
    "DeepRetrievalError",
    "DeepRetrievalLLM",
    "RetrievalQuery",
    "run_deep_retrieve",
]
