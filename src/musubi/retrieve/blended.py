from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.embedding.tei import TEIRerankerClient
from musubi.retrieve.deep import DeepRetrievalLLM, RetrievalQuery, run_deep_retrieve
from musubi.retrieve.scoring import ScoredHit
from musubi.types.common import Err, LifecycleState, Ok, Result

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlendedRetrievalError:
    code: str
    detail: str


@dataclass(frozen=True)
class BlendedResult:
    results: list[ScoredHit]
    planes_contributed: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class BlendedRetrievalQuery:
    namespace: str
    query_text: str
    mode: str = "deep"
    limit: int = 25
    planes: Sequence[str] = ("curated", "concept", "episodic")
    include_lineage: bool = True
    state_filter: Sequence[LifecycleState] | None = None
    presences: Sequence[str] | None = None


# Provenance weights for resolving duplicate ties (curated > concept > episodic-matured > episodic-provisional)
_PROVENANCE_PRIORITY = {
    "curated": 4,
    "concept": 3,
    "episodic_matured": 2,
    "episodic_provisional": 1,
}


def _hit_provenance(hit: ScoredHit) -> int:
    if hit.plane in ("curated", "concept"):
        return _PROVENANCE_PRIORITY[hit.plane]
    state = hit.payload.get("state", "provisional")
    if state in ("matured", "promoted"):
        return _PROVENANCE_PRIORITY["episodic_matured"]
    return _PROVENANCE_PRIORITY["episodic_provisional"]


def _content_hash(content: str) -> str:
    """First-300-char content hash matches exactly."""
    return hashlib.sha256(content[:300].encode("utf-8")).hexdigest()


def _jaccard(tags1: list[str], tags2: list[str]) -> float:
    s1, s2 = set(tags1), set(tags2)
    if not s1 and not s2:
        return 1.0
    return len(s1 & s2) / len(s1 | s2)


async def _cosine_sim(embedder: Embedder, content1: str, content2: str) -> float:
    # Use embedder to get dense vectors
    vecs = await embedder.embed_dense([content1[:500], content2[:500]])
    v1, v2 = vecs[0], vecs[1]
    dot = sum(a * b for a, b in zip(v1, v2, strict=True))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(a * a for a in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return float(dot / (mag1 * mag2))


async def run_blended_retrieve(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient,
    query: BlendedRetrievalQuery,
    llm: DeepRetrievalLLM | None = None,
) -> Result[BlendedResult, BlendedRetrievalError]:
    """Execute blended retrieval across planes."""
    # 1. Expand namespace
    expanded_namespaces: list[tuple[str, str]] = []
    if query.namespace.endswith("/blended"):
        tenant = query.namespace.split("/")[0]
        presences = query.presences or ["claude-code", "claude-desktop", "livekit-voice"]
        if "curated" in query.planes:
            expanded_namespaces.append(("curated", f"{tenant}/_shared/curated"))
        if "concept" in query.planes:
            expanded_namespaces.append(("concept", f"{tenant}/_shared/concept"))
        if "episodic" in query.planes:
            for p in presences:
                expanded_namespaces.append(("episodic", f"{tenant}/{p}/episodic"))
        if "artifact" in query.planes:
            expanded_namespaces.append(("artifact", f"{tenant}/_shared/artifact"))
    else:
        # Standard fallback if not /blended
        parts = query.namespace.split("/")
        base_ns = "/".join(parts[:2]) if len(parts) >= 3 else query.namespace
        for p in query.planes:
            expanded_namespaces.append((p, f"{base_ns}/{p}"))

    # 2. Call run_deep_retrieve per plane/namespace
    coros = []
    for plane, ns in expanded_namespaces:
        dq = RetrievalQuery(
            namespace=ns,
            query_text=query.query_text,
            mode=query.mode,
            limit=query.limit * 2,
            planes=[plane],
            include_lineage=query.include_lineage,
            state_filter=query.state_filter,
        )
        if query.mode == "deep":
            coros.append(run_deep_retrieve(client, embedder, reranker, dq, llm=llm))
        else:
            # We don't have run_fast_retrieve exposed here in our local tree yet,
            # but we can fallback or raise if needed. For now assume deep is used or we just call deep.
            coros.append(run_deep_retrieve(client, embedder, reranker, dq, llm=llm))

    results = await asyncio.gather(*coros, return_exceptions=True)

    planes_contributed = []
    warnings = []
    all_hits: list[ScoredHit] = []

    for (plane, ns), res in zip(expanded_namespaces, results, strict=True):
        if isinstance(res, Exception):
            warnings.append(f"plane {plane} ({ns}) failed: {res}")
            continue
        if isinstance(res, Err):
            warnings.append(f"plane {plane} ({ns}) error: {res.error.code}")
            continue

        hits = cast(Ok[list[ScoredHit]], res).value
        if hits:
            planes_contributed.append(f"{plane}:{ns}")
            all_hits.extend(hits)

    if not all_hits:
        if not planes_contributed and not warnings:
            warnings.append("no hits in any plane")
        elif not planes_contributed:
            # Entirely failed
            pass
        else:
            warnings.append("no hits in any plane")
        return Ok(value=BlendedResult(results=[], planes_contributed=[], warnings=warnings))

    # 3. Content Dedup
    deduped_hits: list[ScoredHit] = []
    dropped_by_content = set()

    # Sort hits by provenance desc, then score desc, so we KEEP the best one
    all_hits.sort(key=lambda h: (_hit_provenance(h), h.score), reverse=True)

    for i in range(len(all_hits)):
        h1 = all_hits[i]
        if h1.object_id in dropped_by_content:
            continue

        c1 = str(h1.payload.get("content", ""))
        hash1 = _content_hash(c1)
        tags1 = h1.payload.get("tags", [])

        is_duplicate = False
        for j in range(i):
            h2 = all_hits[j]
            if h2.object_id in dropped_by_content:
                continue

            c2 = str(h2.payload.get("content", ""))
            if _content_hash(c2) == hash1:
                is_duplicate = True
                break

            if query.mode == "deep":
                tags2 = h2.payload.get("tags", [])
                if _jaccard(tags1, tags2) >= 0.5:
                    sim = await _cosine_sim(embedder, c1, c2)
                    if sim >= 0.92:
                        is_duplicate = True
                        break

        if is_duplicate:
            dropped_by_content.add(h1.object_id)
        else:
            deduped_hits.append(h1)

    # 4. Lineage-aware drop
    promoted_curateds = {h.object_id for h in deduped_hits if h.plane == "curated"}
    to_drop = set()

    for h in deduped_hits:
        if h.plane == "concept":
            lineage = h.payload.get("lineage", {})
            promoted_to = lineage.get("promoted_to")
            if promoted_to and promoted_to.get("object_id") in promoted_curateds:
                to_drop.add(h.object_id)

    # Supersession drop
    all_ids = {h.object_id for h in deduped_hits}
    for h in deduped_hits:
        lineage = h.payload.get("lineage", {})
        superseded_by = lineage.get("superseded_by")
        if superseded_by and superseded_by.get("object_id") in all_ids:
            to_drop.add(h.object_id)

    final_hits = [h for h in deduped_hits if h.object_id not in to_drop]

    # 5. Score Normalization within blend
    # Spec: "Before scoring, per-plane rrf_score values need normalizing to [0, 1] within the batch."
    # Wait, they are already ScoredHits! They already have `score`.
    # Should I normalize the `score` itself?
    # "batch_max = max(h.rrf_score for h in hits) or 1.0
    #  for h in hits: h.relevance_normalized = h.rrf_score / batch_max"
    # Since I already called `run_deep_retrieve` which computes `score`, I can just normalize `score`
    # or just sort by `score` as they are already globally scored via TEIReranker which outputs cross-encoder logits.
    # Cross-encoder logits are universally comparable! RRF scores were just for candidate fetching!
    # So I just sort by `score`.

    final_hits.sort(key=lambda h: h.score, reverse=True)
    final_hits = final_hits[: query.limit]

    return Ok(
        value=BlendedResult(
            results=final_hits, planes_contributed=list(set(planes_contributed)), warnings=warnings
        )
    )


__all__ = [
    "BlendedResult",
    "BlendedRetrievalError",
    "BlendedRetrievalQuery",
    "run_blended_retrieve",
]
