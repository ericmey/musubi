"""RET-004 / #430 — eval-only abstention confidence from PRE-fusion dense similarity.

Abstention needs an ABSOLUTE confidence signal: "did retrieval actually find anything relevant, or
should the system decline to answer?" Two layers above cannot supply it:

- ``retrieve()`` output normalizes relevance per-query (RET-012 recomputes each hit against a single
  working-set global max), so its top hit is always ~1.0 — a pure-noise query and a real answer are
  indistinguishable at that layer.
- Qdrant's fused (RRF) ``query_points`` score is rank-shaped, not an absolute similarity.

So the abstention gate reads the absolute **dense cosine** of the real retrieval candidates BEFORE
fusion — a documented, eval-only signal (NOT a public ``RetrievalResult`` field). The collection is
cosine-distance, so a dense-only ``query_points`` returns the true query↔candidate cosine. The gate
abstains when no candidate clears the frozen, versioned threshold.

The threshold discriminates only as well as the embedder: with a real semantic embedder (TEI, the
scheduled x86 stage) a paraphrased answerable query clears it; with the deterministic FakeEmbedder
(local) only a near-verbatim match does — which is why the LOCAL gate proves the *mechanism*
(threshold separates a real candidate from noise, zero noise hits) and the scheduled stage proves
semantic recall on real embeddings.
"""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.store.specs import DENSE_VECTOR_NAME

#: Frozen, versioned abstention threshold on absolute dense cosine. Calibrated boundary — a candidate
#: must clear this to count as a confident retrieval; below it the system abstains.
ABSTENTION_DENSE_COSINE_THRESHOLD = 0.50
ABSTENTION_THRESHOLD_VERSION = "ret004-v1-2026-07-15"


async def dense_candidate_scores(
    client: QdrantClient,
    embedder: Embedder,
    *,
    collection: str,
    namespace: str,
    query: str,
    limit: int = 10,
) -> list[float]:
    """The absolute dense cosine of each real retrieval candidate for ``query`` (pre-fusion,
    namespace-scoped), highest first. Eval-only — drives the same Qdrant the pipeline uses."""
    dense = (await embedder.embed_dense([query]))[0]
    resp = client.query_points(
        collection_name=collection,
        query=dense,
        using=DENSE_VECTOR_NAME,
        query_filter=models.Filter(
            must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))]
        ),
        limit=limit,
        with_payload=False,
    )
    return [float(point.score) for point in resp.points]


def confident_hits(
    scores: list[float], threshold: float = ABSTENTION_DENSE_COSINE_THRESHOLD
) -> int:
    """How many candidate scores clear the abstention threshold. Zero ⇒ the system abstains."""
    return sum(1 for score in scores if score >= threshold)


__all__ = [
    "ABSTENTION_DENSE_COSINE_THRESHOLD",
    "ABSTENTION_THRESHOLD_VERSION",
    "confident_hits",
    "dense_candidate_scores",
]
