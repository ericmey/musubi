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

from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.embedding.tei import TEIRerankerClient
from musubi.planes.artifact.plane import ArtifactPlane
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.hybrid import HybridHit, hybrid_search
from musubi.retrieve.rerank import rerank
from musubi.retrieve.scoring import Hit, ScoredHit, rank_hits
from musubi.store.names import collection_for_plane
from musubi.types.common import Err, LifecycleState, Ok, Result, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeepRetrievalError:
    code: str
    detail: str


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
) -> Result[list[ScoredHit], DeepRetrievalError]:
    """Execute deep-path retrieval.

    Orchestrates hybrid_search -> rerank -> LLM expansion -> score -> lineage.
    """
    if llm is None:
        llm = _NotConfiguredDeepLLM()

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
                # Give generous timeouts for deep path
                timeout_s=5.0,
                sparse_timeout_s=1.0,
            )
        )

    results = await asyncio.gather(*hybrid_coros)

    errors = [res.error for res in results if isinstance(res, Err)]
    if errors:
        return Err(error=DeepRetrievalError(code=errors[0].code, detail=errors[0].detail))

    # Merge and dedup hits
    merged: dict[str, HybridHit] = {}
    for res in results:
        for hit in cast(Ok[list[HybridHit]], res).value:
            previous = merged.get(hit.object_id)
            if previous is None or hit.score > previous.score:
                merged[hit.object_id] = hit

    hybrid_hits = list(merged.values())
    if not hybrid_hits:
        return Ok(value=[])

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
    reranked_hits = await rerank(
        client=reranker,
        query_text=query.query_text,
        candidates=hits,
        top_k=query.limit,
    )

    # 5. Score
    now = utc_now().timestamp()
    scored = rank_hits(reranked_hits, now=now)

    # 6. Hydrate Lineage
    if query.include_lineage and scored:
        hydrate_coros = [_hydrate_one(hit, client, embedder) for hit in scored]
        # Use return_exceptions to degrade gracefully if a plane times out
        hydrated_results = await asyncio.gather(*hydrate_coros, return_exceptions=True)

        final_scored: list[ScoredHit] = []
        for i, hydrated_res in enumerate(hydrated_results):
            if isinstance(hydrated_res, Exception):
                logger.warning(f"Lineage hydrate failed for {scored[i].object_id}: {hydrated_res}")
                final_scored.append(scored[i])
            else:
                final_scored.append(cast(ScoredHit, hydrated_res))
        scored = final_scored

    return Ok(value=scored)


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
        obj = await asyncio.wait_for(
            episodic.get(namespace=ns, object_id=hit.object_id), timeout=1.0
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
            current = await episodic.get(namespace=ns, object_id=nxt_id)
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


__all__ = ["DeepRetrievalError", "DeepRetrievalLLM", "RetrievalQuery", "run_deep_retrieve"]
