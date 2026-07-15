"""RET-004 successor — the LIVE scheduled quality gate.

Runs the checksum-pinned golden corpus through REAL retrieval (Qdrant + TEI) and enforces the
mode-specific absolute thresholds (fast vs deep MRR / NDCG@10 / Recall@20 / P@1) via the foundation's
:func:`musubi.evals.gates.check_nightly_thresholds`.

This is NOT the deterministic PR-smoke gate and it does NOT fabricate numbers: when TEI is
unavailable it FAILS LOUD (:class:`LiveGateUnavailable`) so the scheduled command exits non-zero
rather than emitting an empty or invented result. The real quality NUMBERS are proven only on the
scheduled x86 TEI CI; on a TEI-less box only the fail-loud path runs.

The metric core (:func:`evaluate_query`, :func:`aggregate`, :func:`run_live_gate`,
:func:`enforce_thresholds`) takes an injected retriever, so it is deterministic and unit-testable
without TEI. :func:`build_settings_retriever` is the real Qdrant+TEI boundary, exercised on CI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from musubi.evals.gates import check_nightly_thresholds
from musubi.evals.metrics import ndcg_at_k, recall_at_k, rr

#: A retriever maps a golden query (dict with ``mode``/``namespace``/``text``) to the RANKED
#: ``object_id`` list the live pipeline returned for it. The injected boundary to real retrieval.
Retriever = Callable[[dict[str, Any]], Awaitable[list[str]]]

_NDCG_K = 10
_RECALL_K = 20
_PERFECT_GRADE = 3  # evals.md P@1 = "is the first result perfect (relevance == 3)?"


class LiveGateUnavailable(RuntimeError):
    """The live gate could not reach its required TEI/Qdrant backends. Raised so the scheduled
    command FAILS LOUD (non-zero) rather than emitting fabricated or empty quality numbers."""


def evaluate_query(ordered_ids: list[str], relevant: list[dict[str, Any]]) -> dict[str, float]:
    """Graded retrieval metrics for one query from its ranked hit ids and its 0-3 relevance labels.

    ``ordered_ids`` is the pipeline's ranked ``object_id`` list; ``relevant`` is the golden query's
    ``[{object_id, relevance}, ...]``. Non-relevant hits contribute grade 0.
    """
    grade_by_id = {str(item["object_id"]): int(item["relevance"]) for item in relevant}
    ranked = [grade_by_id.get(object_id, 0) for object_id in ordered_ids]
    ideal = list(grade_by_id.values())
    total_relevant = sum(1 for grade in grade_by_id.values() if grade > 0)
    return {
        "ndcg@10": ndcg_at_k(ranked, ideal, _NDCG_K),
        "mrr": rr(ranked),
        "recall@20": recall_at_k(ranked, total_relevant, _RECALL_K),
        "p@1": 1.0 if ranked and ranked[0] == _PERFECT_GRADE else 0.0,
    }


def aggregate(per_query: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each metric across a query set. An empty set returns ``{}`` — the caller treats a
    mode with no queries as a hard failure, never a vacuous pass."""
    if not per_query:
        return {}
    return {
        metric: sum(row[metric] for row in per_query) / len(per_query) for metric in per_query[0]
    }


async def run_live_gate(
    queries: list[dict[str, Any]], retriever: Retriever
) -> dict[str, dict[str, float]]:
    """Retrieve every query through ``retriever`` and aggregate metrics per ``mode``.

    Grouping by mode lets :func:`enforce_thresholds` apply the fast/deep-specific targets. A
    retriever that raises :class:`LiveGateUnavailable` (TEI down mid-run) propagates unchanged — the
    gate fails loud, never partially fabricates.
    """
    by_mode: dict[str, list[dict[str, float]]] = {}
    for query in queries:
        ordered_ids = await retriever(query)
        metrics = evaluate_query(ordered_ids, list(query.get("relevant", [])))
        by_mode.setdefault(str(query["mode"]), []).append(metrics)
    return {mode: aggregate(rows) for mode, rows in by_mode.items()}


def enforce_thresholds(by_mode: dict[str, dict[str, float]]) -> None:
    """Enforce the mode-specific absolute thresholds; raise ``ValueError`` naming the first breach.

    Reuses the foundation's :func:`check_nightly_thresholds` so the successor never re-derives the
    numbers. No metrics at all — or none for a mode — is itself a hard failure (nothing ran)."""
    if not by_mode:
        raise ValueError("live gate produced no metrics — corpus empty or every query failed")
    for mode, metrics in by_mode.items():
        if not metrics:
            raise ValueError(f"live gate produced no metrics for mode {mode!r}")
        check_nightly_thresholds(metrics, mode)  # raises ValueError below threshold / on non-finite


#: A hybrid-vs-dense search maps ``(query, hybrid?)`` to the RANKED ``object_id`` list — ``hybrid``
#: True runs the fused dense+sparse pipeline, False runs dense-only. The injected boundary the BEIR
#: contract measures.
HybridDenseSearch = Callable[[dict[str, Any], bool], Awaitable[list[str]]]

#: The BEIR contract: hybrid NDCG@10 must beat dense-only by at least this margin.
BEIR_MIN_HYBRID_DENSE_DELTA = 0.02


async def measure_hybrid_vs_dense(
    queries: list[dict[str, Any]], search: HybridDenseSearch
) -> dict[str, float]:
    """Mean NDCG@10 of hybrid vs dense-only retrieval over the SAME graded corpus.

    Returns ``{"hybrid_ndcg@10", "dense_ndcg@10", "delta"}``. The BEIR gate asserts
    ``delta >= BEIR_MIN_HYBRID_DENSE_DELTA``. ``search`` is the injected retrieval boundary so the
    comparison mechanics are deterministic and unit-testable; the real 1000-doc numbers (which
    actually separate hybrid from dense) come from the live TEI+Qdrant pipeline on the scheduled CI.
    """
    hybrid_rows: list[dict[str, float]] = []
    dense_rows: list[dict[str, float]] = []
    for query in queries:
        relevant = list(query.get("relevant", []))
        hybrid_rows.append(evaluate_query(await search(query, True), relevant))
        dense_rows.append(evaluate_query(await search(query, False), relevant))
    hybrid_ndcg = aggregate(hybrid_rows).get("ndcg@10", 0.0)
    dense_ndcg = aggregate(dense_rows).get("ndcg@10", 0.0)
    return {
        "hybrid_ndcg@10": hybrid_ndcg,
        "dense_ndcg@10": dense_ndcg,
        "delta": hybrid_ndcg - dense_ndcg,
    }


class _TEIComposite:
    """Minimal :class:`~musubi.embedding.base.Embedder` composing the dense + sparse + reranker TEI
    clients so :class:`~musubi.embedding.chunked.ChunkedEmbedder` can wrap them. Each retrieval entry
    point in the tree wires its own TEI clients; this keeps the live gate from importing the API
    bootstrap internals."""

    def __init__(self, *, dense: Any, sparse: Any, reranker: Any) -> None:
        self._dense = dense
        self._sparse = sparse
        self._reranker = reranker

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = await self._dense.embed_dense(texts)
        return vectors

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        vectors: list[dict[int, float]] = await self._sparse.embed_sparse(texts)
        return vectors

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        scores: list[float] = await self._reranker.rerank(query, candidates)
        return scores


def build_settings_retriever() -> Retriever:
    """Construct a retriever backed by REAL Qdrant + TEI from :class:`~musubi.settings.Settings`,
    probing the dense TEI endpoint up front so an unavailable stack fails loud immediately rather
    than mid-corpus. Mirrors the wiring in :mod:`musubi.api.bootstrap`. CI-exercised path."""
    import asyncio

    from qdrant_client import QdrantClient

    from musubi.embedding import TEIDenseClient, TEIRerankerClient, TEISparseClient
    from musubi.embedding.base import EmbeddingError
    from musubi.embedding.chunked import ChunkedEmbedder
    from musubi.retrieve.deep import RetrievalQuery, run_deep_retrieve
    from musubi.retrieve.fast import run_fast_retrieve
    from musubi.settings import Settings

    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # unconfigured settings → cannot run the live gate
        raise LiveGateUnavailable(f"live gate settings unavailable: {exc}") from exc

    dense = TEIDenseClient(base_url=str(settings.tei_dense_url))
    sparse = TEISparseClient(base_url=str(settings.tei_sparse_url))
    reranker = TEIRerankerClient(base_url=str(settings.tei_reranker_url))
    embedder = ChunkedEmbedder(_TEIComposite(dense=dense, sparse=sparse, reranker=reranker))
    client = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
    )

    # Fail loud NOW if TEI is unreachable — never begin a run that would emit fabricated numbers.
    try:
        asyncio.run(embedder.embed_dense(["live-gate readiness probe"]))
    except EmbeddingError as exc:
        raise LiveGateUnavailable(f"TEI dense endpoint unavailable: {exc}") from exc

    async def retrieve(query: dict[str, Any]) -> list[str]:
        namespace = str(query["namespace"])
        text = str(query["text"])
        try:
            if str(query["mode"]) == "deep":
                result: Any = await run_deep_retrieve(
                    client,
                    embedder,
                    reranker,
                    RetrievalQuery(
                        namespace=namespace, query_text=text, mode="deep", limit=_RECALL_K
                    ),
                )
            else:
                result = await run_fast_retrieve(
                    client, embedder, namespace=namespace, query=text, limit=_RECALL_K
                )
        except EmbeddingError as exc:
            raise LiveGateUnavailable(f"TEI unavailable mid-run: {exc}") from exc
        if not result.is_ok:
            raise LiveGateUnavailable(
                f"retrieval failed for query {query.get('id')!r}: {result.error}"
            )
        return [hit.object_id for hit in result.value.results]

    return retrieve


__all__ = [
    "BEIR_MIN_HYBRID_DENSE_DELTA",
    "HybridDenseSearch",
    "LiveGateUnavailable",
    "Retriever",
    "aggregate",
    "build_settings_retriever",
    "enforce_thresholds",
    "evaluate_query",
    "measure_hybrid_vs_dense",
    "run_live_gate",
]
