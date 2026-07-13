"""Cross-encoder reranking for the retrieval deep path."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from musubi.embedding.base import EmbeddingError
from musubi.embedding.tei import TEIRerankerClient
from musubi.retrieve.warnings import RetrievalWarning, reranker_failed

if TYPE_CHECKING:
    from musubi.retrieve.scoring import Hit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankResult:
    """The result of :func:`rerank`: the top-k ``hits`` plus a RET-007 ``reranker_failed`` warning
    when the cross-encoder degraded and the ranking fell back to the fused (RRF) order."""

    hits: list[Hit]
    warnings: tuple[RetrievalWarning, ...] = ()


async def rerank(
    client: TEIRerankerClient,
    query_text: str,
    candidates: list[Hit],
    *,
    top_k: int,
) -> RerankResult:
    """Score candidates via cross-encoder and return top-k.

    If candidate count <= 5, returns input list as-is (reranking is overkill
    for tiny result sets).
    """
    if len(candidates) <= 5:
        return RerankResult(hits=candidates[:top_k])

    # A rerank degradation is not plane-specific; within a deep leg the candidates share one plane, so
    # attribute the warning to that plane (candidates is non-empty here — len > 5).
    plane = candidates[0].plane

    try:
        texts = [_extract_content(c) for c in candidates]
        scores = await client.rerank(query_text, texts)
    except EmbeddingError as exc:
        logger.warning(
            "Reranker failed (TEI down or timeout); falling back to hybrid-only. error=%s",
            exc,
        )
        return RerankResult(hits=candidates[:top_k], warnings=(reranker_failed(plane),))
    except Exception as exc:
        logger.error("Unexpected error in reranker: %s", exc, exc_info=True)
        return RerankResult(hits=candidates[:top_k], warnings=(reranker_failed(plane),))

    # Apply scores to new Hit instances (original is frozen)
    scored = [replace(c, rerank_score=score) for c, score in zip(candidates, scores, strict=True)]

    # Sort by rerank_score descending.
    # We don't use full scoring.rank_hits here because the spec says:
    # "ranked = sorted(candidates, key=lambda c: c.rerank_score, reverse=True)"
    # but also "return ranked[:top_k]".
    # Note: scoring.rank_hits will be called downstream on the reranked list.
    ranked = sorted(scored, key=lambda c: c.rerank_score or -1e9, reverse=True)
    return RerankResult(hits=ranked[:top_k])


def _extract_content(hit: Hit) -> str:
    """Extract rerankable text from a hit payload.

    - episodic/concept/curated: title + content
    - artifact: chunk_content
    """
    payload = hit.payload
    if hit.plane == "artifact":
        return str(payload.get("chunk_content", ""))

    title = payload.get("title", "")
    content = payload.get("content", "")
    if title:
        return f"{title}\n\n{content[:2048]}"
    return str(content[:2048])
