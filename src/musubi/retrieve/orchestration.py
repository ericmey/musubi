"""The top-level retrieval orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, ValidationError
from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.embedding.tei import TEIRerankerClient
from musubi.retrieve.blended import (
    BlendedRetrievalQuery,
    run_blended_retrieve,
)
from musubi.retrieve.deep import (
    DeepRetrievalLLM,
    run_deep_retrieve,
)
from musubi.retrieve.deep import (
    RetrievalQuery as DeepRetrievalQuery,
)
from musubi.retrieve.fast import run_fast_retrieve
from musubi.types.common import Err, LifecycleState, Ok, Result

logger = logging.getLogger(__name__)


class NamespaceTarget(BaseModel):
    """One concrete (namespace, plane) target produced by the router's
    shape resolution. 3-segment body → a single target; 2-segment body
    → one target per plane. Orchestration iterates these."""

    namespace: str = Field(min_length=1)
    plane: str = Field(min_length=1)


class RetrievalQuery(BaseModel):
    namespace: str = Field(min_length=1)
    query_text: str = Field(min_length=1)
    mode: Literal["fast", "deep", "blended"] = "deep"
    limit: int = Field(default=25, ge=1, le=100)
    planes: list[str] = Field(default_factory=lambda: ["curated", "concept", "episodic"])
    include_lineage: bool = True
    include_archived: bool = False
    presences: list[str] | None = None
    state_filter: list[LifecycleState] | None = None
    #: Per-plane namespace fanout. When set, each target drives one
    #: single-plane pipeline run and results are merged by score.
    #: Set by the router from :func:`retrieve._resolve_targets` — callers
    #: that use the orchestrator directly can leave it unset and the
    #: orchestrator derives a default single-plane target from the
    #: top-level ``namespace``.
    namespace_targets: list[NamespaceTarget] | None = None


class RetrievalResult(BaseModel):
    object_id: str
    namespace: str
    plane: str
    title: str | None = None
    snippet: str
    score: float
    score_components: dict[str, float]
    lineage: dict[str, Any]
    payload: dict[str, Any] | None = None


class RetrievalError(BaseModel):
    kind: Literal["bad_query", "forbidden", "timeout", "internal"]
    detail: str
    warnings: list[str] = Field(default_factory=list)


async def retrieve(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient | None = None,
    *,
    query: RetrievalQuery | dict[str, Any],
    llm: DeepRetrievalLLM | None = None,
    now: float | None = None,
) -> Result[list[RetrievalResult], RetrievalError]:
    """Execute the configured retrieval pipeline based on the query."""

    # 1. validate query
    if isinstance(query, dict):
        try:
            parsed_query = RetrievalQuery.model_validate(query)
        except ValidationError as e:
            return Err(error=RetrievalError(kind="bad_query", detail=str(e)))
    else:
        try:
            parsed_query = RetrievalQuery.model_validate(query.model_dump())
        except ValidationError as e:
            return Err(error=RetrievalError(kind="bad_query", detail=str(e)))

    # Basic auth check handled upstream, here we just dispatch.
    # Expand the query into per-plane pipeline runs when the router
    # supplied explicit `namespace_targets`; otherwise derive a single
    # target from the top-level namespace (legacy path — orchestrator
    # callers that don't go through the HTTP router).
    if parsed_query.namespace_targets:
        targets = [(t.namespace, t.plane) for t in parsed_query.namespace_targets]
    else:
        derived_plane = (
            parsed_query.namespace.rsplit("/", 1)[-1]
            if parsed_query.namespace.count("/") == 2
            else (parsed_query.planes[0] if parsed_query.planes else "episodic")
        )
        targets = [(parsed_query.namespace, derived_plane)]

    # Single-target fast path preserves the current behaviour bit-for-
    # bit (one pipeline run, one Qdrant query per collection, no merge).
    # Multi-target path runs the same pipeline per target concurrently
    # and merges ranked results by score at the end.
    if len(targets) == 1:
        return await _run_single(
            client=client,
            embedder=embedder,
            reranker=reranker,
            llm=llm,
            parsed_query=parsed_query,
            namespace=targets[0][0],
            plane=targets[0][1],
            now=now,
        )

    # Fan out — one pipeline run per (namespace, plane) target. Call
    # `_run_single` directly rather than recursing through `retrieve()`:
    # the query is already parsed + validated, and re-entering the
    # top-level would redo target expansion, pydantic validation, and
    # the single-vs-multi branch logic for each leg. `gather(return_
    # exceptions=True)` so a single plane failing doesn't blank the
    # whole cross-plane response (ADR 0028).
    results_per_target = await asyncio.gather(
        *(
            _run_single(
                client=client,
                embedder=embedder,
                reranker=reranker,
                llm=llm,
                parsed_query=parsed_query,
                namespace=ns,
                plane=plane,
                now=now,
            )
            for ns, plane in targets
        ),
        return_exceptions=True,
    )

    merged: list[RetrievalResult] = []
    seen: set[str] = set()
    transient_any = False
    internal_err: RetrievalError | None = None
    for outcome in results_per_target:
        if isinstance(outcome, Err):
            # Per-plane failures: transient/timeout degrades per-plane;
            # an internal error from *any* plane surfaces as a 5xx
            # because the merged response would silently under-report.
            if outcome.error.kind == "timeout":
                transient_any = True
                continue
            if outcome.error.kind in ("internal", "bad_query"):
                internal_err = outcome.error
                break
            continue
        if isinstance(outcome, Ok):
            for hit in outcome.value:
                if hit.object_id in seen:
                    continue
                seen.add(hit.object_id)
                merged.append(hit)
            continue
        if isinstance(outcome, BaseException):
            logger.warning("cross-plane retrieve per-plane exception: %r", outcome)
            internal_err = RetrievalError(kind="internal", detail=str(outcome))
            break

    if internal_err is not None:
        return Err(error=internal_err)
    if not merged and transient_any:
        return Err(error=RetrievalError(kind="timeout", detail="all planes timed out"))

    merged.sort(key=lambda r: r.score, reverse=True)
    return Ok(value=merged[: parsed_query.limit])


async def _run_single(
    *,
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient | None,
    llm: DeepRetrievalLLM | None,
    parsed_query: RetrievalQuery,
    namespace: str,
    plane: str,
    now: float | None,
) -> Result[list[RetrievalResult], RetrievalError]:
    """Single-target pipeline dispatch. Extracted from :func:`retrieve`
    so cross-plane fanout can call it per target without re-parsing
    the query shape. Behaviour is identical to the pre-fanout code —
    a single call with ``planes=[plane]`` and the given namespace."""

    mode = parsed_query.mode
    warnings: list[str] = []
    # Force single-plane, single-namespace for this leg. The input
    # `parsed_query.planes` may carry the full cross-plane list from
    # the top-level query; we use only the plane this leg owns.
    legs_planes: list[str] = [plane]
    target_namespace = namespace

    try:
        if mode == "blended":
            if reranker is None:
                return Err(
                    error=RetrievalError(
                        kind="internal", detail="TEIRerankerClient is required for blended mode"
                    )
                )

            blended_query = BlendedRetrievalQuery(
                namespace=target_namespace,
                query_text=parsed_query.query_text,
                mode="deep",  # Blended internally runs deep
                limit=parsed_query.limit,
                planes=legs_planes,
                include_lineage=parsed_query.include_lineage,
                state_filter=parsed_query.state_filter,
                presences=parsed_query.presences,
            )

            # Blended timeout (5s)
            b_res = await asyncio.wait_for(
                run_blended_retrieve(
                    client=client,
                    embedder=embedder,
                    reranker=reranker,
                    query=blended_query,
                    llm=llm,
                ),
                timeout=5.0,
            )
            if isinstance(b_res, Err):
                # map error
                return Err(error=RetrievalError(kind="internal", detail=b_res.error.detail))

            warnings.extend(b_res.value.warnings)
            return Ok(
                value=_pack_scored_hits(
                    b_res.value.results,
                    warnings,
                    include_payload=not getattr(parsed_query, "brief", False),
                )
            )

        elif mode == "deep":
            if reranker is None:
                return Err(
                    error=RetrievalError(
                        kind="internal", detail="TEIRerankerClient is required for deep mode"
                    )
                )

            deep_query = DeepRetrievalQuery(
                namespace=target_namespace,
                query_text=parsed_query.query_text,
                mode="deep",
                limit=parsed_query.limit,
                planes=legs_planes,
                include_lineage=parsed_query.include_lineage,
                state_filter=parsed_query.state_filter,
            )

            # Deep timeout (5s)
            d_res = await asyncio.wait_for(
                run_deep_retrieve(
                    client=client,
                    embedder=embedder,
                    reranker=reranker,
                    query=deep_query,
                    llm=llm,
                ),
                timeout=5.0,
            )
            if isinstance(d_res, Err):
                return Err(error=RetrievalError(kind="internal", detail=d_res.error.detail))

            return Ok(
                value=_pack_scored_hits(
                    d_res.value, warnings, include_payload=not getattr(parsed_query, "brief", False)
                )
            )

        elif mode == "fast":
            # Fast timeout (400ms)
            states = parsed_query.state_filter or ("matured", "promoted")
            if parsed_query.include_archived:
                states = cast(Any, (*states, "demoted", "archived", "superseded"))

            f_res = await asyncio.wait_for(
                run_fast_retrieve(
                    client=client,
                    embedder=embedder,
                    namespace=target_namespace,
                    query=parsed_query.query_text,
                    collections=["musubi_" + p for p in legs_planes],
                    limit=parsed_query.limit,
                    now=now,
                    state_filter=cast(Any, states),
                ),
                timeout=0.400,
            )

            if isinstance(f_res, Err):
                return Err(error=RetrievalError(kind="internal", detail=f_res.error.detail))

            warnings.extend(f_res.value.warnings)

            results = []
            for hit in f_res.value.results:
                results.append(
                    RetrievalResult(
                        object_id=hit.object_id,
                        namespace=target_namespace,
                        plane=str(hit.payload.get("plane", "episodic")),
                        title=hit.payload.get("title"),
                        snippet=hit.snippet,
                        score=hit.score,
                        score_components={
                            "relevance": hit.score_components.relevance,
                            "recency": hit.score_components.recency,
                            "reinforcement": hit.score_components.reinforce,
                        },
                        lineage=hit.lineage_summary,
                        payload=hit.payload,
                    )
                )
            return Ok(value=results)

        else:
            return Err(error=RetrievalError(kind="bad_query", detail=f"Unknown mode: {mode}"))

    except TimeoutError:
        return Err(error=RetrievalError(kind="timeout", detail=f"{mode} retrieval timed out"))
    except Exception as e:
        logger.error("Internal retrieval error: %s", e, exc_info=True)
        return Err(error=RetrievalError(kind="internal", detail=str(e)))


def _pack_scored_hits(
    hits: Sequence[Any], warnings: list[str], include_payload: bool
) -> list[RetrievalResult]:
    results = []
    for hit in hits:
        results.append(
            RetrievalResult(
                object_id=hit.object_id,
                namespace=hit.payload.get("namespace", ""),
                plane=hit.plane,
                title=hit.payload.get("title"),
                snippet=_snippet(hit.payload, max_chars=300),
                score=hit.score,
                score_components={
                    "relevance": hit.score_components.relevance,
                    "recency": hit.score_components.recency,
                    "reinforcement": hit.score_components.reinforce,
                },
                lineage=_summarize_lineage(hit.payload),
                payload=hit.payload if include_payload else None,
            )
        )
    return results


def _snippet(payload: dict[str, Any], max_chars: int) -> str:
    content = str(payload.get("content") or payload.get("title") or "")
    return content[:max_chars]


def _summarize_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    lineage = payload.get("lineage")
    summary = dict(lineage) if isinstance(lineage, dict) else {}
    for key in ("promoted_to", "promoted_from", "supersedes", "superseded_by"):
        value = payload.get(key)
        if value is not None:
            summary[key] = value
    return {key: value for key, value in summary.items() if value is not None}
