"""The top-level retrieval orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, ValidationError, model_validator
from qdrant_client import QdrantClient

from musubi.embedding.base import Embedder
from musubi.embedding.tei import TEIRerankerClient
from musubi.observability import get_tracer
from musubi.observability.retrieval_metrics import (
    RETRIEVAL_ERRORS_TOTAL,
    RETRIEVAL_WARNINGS_TOTAL,
)
from musubi.retrieve.accounting import account_delivered
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
from musubi.retrieve.recent import _provenance_score_for, run_recent_retrieve
from musubi.retrieve.warnings import (
    RetrievalWarning,
    dedupe,
    is_allowlisted,
    plane_error,
    plane_timeout,
)
from musubi.types.common import Err, LifecycleState, Ok, Result

logger = logging.getLogger(__name__)
# Tracer for hand-instrumented spans matching the named hierarchy in
# [[09-operations/observability]] § Tracing. When tracing is disabled
# this is a no-op tracer; ``start_as_current_span`` is safe to call
# unconditionally.
_tracer = get_tracer("musubi.retrieve")


class NamespaceTarget(BaseModel):
    """One concrete (namespace, plane) target produced by the router's
    shape resolution. 3-segment body → a single target; 2-segment body
    → one target per plane. Orchestration iterates these."""

    namespace: str = Field(min_length=1)
    plane: str = Field(min_length=1)


class RetrievalQuery(BaseModel):
    namespace: str = Field(min_length=1)
    # query_text is required for fast/deep/blended; ignored for recent.
    # The model_validator below enforces non-empty for the non-recent modes.
    query_text: str = ""
    mode: Literal["fast", "deep", "blended", "recent"] = "deep"
    limit: int = Field(default=25, ge=1, le=100)
    planes: list[str] = Field(default_factory=lambda: ["curated", "concept", "episodic"])
    include_lineage: bool = True
    include_archived: bool = False
    presences: list[str] | None = None
    state_filter: list[LifecycleState] | None = None
    #: Inclusive epoch-seconds floor for ``mode="recent"``. Ignored by
    #: other modes (they rank with their own recency-weighting). Per
    #: [[_slices/slice-retrieve-recent]] design decisions: ``float`` only;
    #: ISO conversion is a one-line client-side call.
    since: float | None = None
    #: Tag-AND filter — a row must contain every listed tag to match.
    #: Composes with ``mode="recent"``; the other modes don't currently
    #: consume this field but accept it without error for forward compat.
    tags: list[str] | None = None
    #: Per-plane namespace fanout. When set, each target drives one
    #: single-plane pipeline run and results are merged by score.
    #: Set by the router from :func:`retrieve._resolve_targets` — callers
    #: that use the orchestrator directly can leave it unset and the
    #: orchestrator derives a default single-plane target from the
    #: top-level ``namespace``.
    namespace_targets: list[NamespaceTarget] | None = None

    @model_validator(mode="after")
    def _require_query_text_for_ranked_modes(self) -> RetrievalQuery:
        """Enforce: fast/deep/blended require non-empty query_text; recent doesn't.

        ``mode="recent"`` with a `query_text` is accept-and-ignore (the
        dispatch branch logs a WARN). The validator is for the inverse —
        a ranked-mode caller without a query_text would silently retrieve
        garbage; reject loudly at the boundary.
        """
        if self.mode != "recent" and not self.query_text.strip():
            raise ValueError(
                f"query_text is required for mode={self.mode!r} (only mode='recent' may omit it)"
            )
        return self


class RetrievalResult(BaseModel):
    """Internal orchestration-layer projection of one retrieval hit.

    The router reads `state` and `importance` from dedicated fields
    (not from `payload`, which is optional and may be `None` for
    `brief=true`). The orchestration layer populates `state` and
    `importance` from the source row BEFORE payload projection (per
    spec §2.3 / §4.6 / §6.6 invalid source semantics: present-valid
    → exact, missing → null, present-invalid → raise server
    integrity error → 500).

    `score_components` is a flat dict of all 5 contributor names (the
    public `reinforcement` name, NOT the internal `reinforce`). The
    router projects this to `extra.score_components` for ranked mode
    (typed `RankedScoreComponents`) or to `extra.score_components: {}`
    for recent mode (typed `RecentScoreComponents`).
    """

    object_id: str
    namespace: str
    plane: str
    title: str | None = None
    snippet: str
    score: float
    score_components: dict[str, float]
    lineage: dict[str, Any]
    payload: dict[str, Any] | None = None
    # RET-003: top-level source-backed state/importance. The
    # orchestrator extracts these from the source row's payload
    # BEFORE the optional payload projection (so `brief=true`
    # preserves them). Source-backed: present-valid → exact,
    # missing → null, present-invalid → 500 (not coerced).
    state: LifecycleState | None = None
    importance: int | None = None
    # RET-003 recent: exact-table-only provenance_score (None if
    # state missing or (plane, state) absent from `_PROVENANCE`).
    # Ranked does NOT use this; ranked uses `score_components["provenance"]`
    # which may legitimately be 0.1 from `_LOW_PROVENANCE_STATES`.
    provenance_score: float | None = None


class RetrievalError(BaseModel):
    kind: Literal["bad_query", "forbidden", "timeout", "internal"]
    detail: str
    warnings: list[str] = Field(default_factory=list)


_ErrorKind = Literal["bad_query", "forbidden", "timeout", "internal"]


def _kind_from_code(code: str) -> _ErrorKind:
    """Map a sub-layer error CODE to the orchestration failure ``kind`` (RET-007 Blocker 2). A
    timeout anywhere in the pipeline (hybrid ``qdrant_timeout``, blended ``all_planes_timeout``) must
    reach ``kind=timeout`` → HTTP 503 — NOT be flattened to ``internal`` → 500."""
    c = code.lower()
    if "timeout" in c:
        return "timeout"
    if "forbidden" in c or "unauthorized" in c:
        return "forbidden"
    if c in {"empty_query", "invalid_limit", "invalid_weights", "invalid_collections", "bad_query"}:
        return "bad_query"
    return "internal"


def _kind_from_status(status: int) -> _ErrorKind:
    """Map a fast-path HTTP status to the orchestration failure ``kind`` (fast already resolves the
    status; preserve its intent rather than re-deriving from a code)."""
    if status == 503:
        return "timeout"
    if status == 400:
        return "bad_query"
    if status == 403:
        return "forbidden"
    return "internal"


@dataclass(frozen=True)
class RetrievalEnvelope:
    """Explicit typed success value of :func:`retrieve`: the ranked ``results`` plus the aggregated,
    deduped RET-007 degradation ``warnings``. Iterable over ``results`` so existing callers that only
    walk the hits keep working (e.g. ``/v1/context``); NOT a list subclass and with NO
    ``__getitem__``/``__len__``, so a ``[:limit]`` slice can never silently drop the warnings."""

    results: list[RetrievalResult]
    warnings: tuple[RetrievalWarning, ...] = ()

    def __iter__(self) -> Iterator[RetrievalResult]:
        return iter(self.results)


def _finalize(
    result: Result[RetrievalEnvelope, RetrievalError],
) -> Result[RetrievalEnvelope, RetrievalError]:
    """THE shared final retrieval boundary (RET-007 Blockers 3+5). Every caller — ``/v1/retrieve``,
    ``/v1/context``, and any direct orchestration caller — passes through here exactly once, so:

    - **Telemetry is counted once here**, not per-router: ``errors_total{kind}`` on a total failure;
      ``warnings_total{warning,plane}`` once per distinct (code, plane) on a degraded success.
    - **Boundedness fails closed**: only allowlisted warnings survive onto the envelope, so a free-text
      or out-of-vocabulary code/plane can NEVER become an unbounded Prometheus label or reach the wire.
    """
    if isinstance(result, Err):
        RETRIEVAL_ERRORS_TOTAL.labels(kind=result.error.kind).inc()
        return result
    warnings = tuple(w for w in dedupe(result.value.warnings) if is_allowlisted(w))
    for w in warnings:
        RETRIEVAL_WARNINGS_TOTAL.labels(warning=w.code, plane=w.plane).inc()
    return Ok(value=RetrievalEnvelope(results=result.value.results, warnings=warnings))


async def retrieve(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient | None = None,
    *,
    query: RetrievalQuery | dict[str, Any],
    llm: DeepRetrievalLLM | None = None,
    now: float | None = None,
    account_access: bool = True,
) -> Result[RetrievalEnvelope, RetrievalError]:
    """Execute the configured retrieval pipeline, then finalize at the shared boundary (telemetry +
    fail-closed bounded warnings). This is the ONE place RET-007 warnings/errors are counted.

    ``account_access`` (default True) accounts access here over the delivered rows — correct for
    ``/v1/retrieve`` and ``/v1/retrieve/stream``, whose delivered set IS this envelope. Callers
    that drop rows AFTER retrieval (``/v1/context`` → ``build_context_pack`` trims by
    max_items/max_chars/filler) pass ``account_access=False`` and account the FINAL surfaced set
    themselves, so trimmed candidates are never counted.
    """
    result = await _retrieve_uncounted(client, embedder, reranker, query=query, llm=llm, now=now)
    finalized = _finalize(result)
    # RET-002 (#500): account access ONCE, over exactly the delivered rows — after
    # fanout/dedup/sort/limit — never on a dropped candidate and independent of lineage
    # hydration. Covers HTTP and streaming (both call this seam). Does not touch results/warnings.
    if account_access and isinstance(finalized, Ok):
        # Fail-LOUD (access accounting drives lifecycle; it must never silently vanish) but honor
        # the Result contract: normalize an accounting failure to a typed Err, never a raw raise.
        try:
            await account_delivered(client, finalized.value.results)
        except Exception as exc:
            return Err(
                error=RetrievalError(
                    kind="internal", detail=f"access accounting failed: {type(exc).__name__}"
                )
            )
    return finalized


async def _retrieve_uncounted(
    client: QdrantClient,
    embedder: Embedder,
    reranker: TEIRerankerClient | None = None,
    *,
    query: RetrievalQuery | dict[str, Any],
    llm: DeepRetrievalLLM | None = None,
    now: float | None = None,
) -> Result[RetrievalEnvelope, RetrievalError]:
    """Execute the configured retrieval pipeline based on the query (no telemetry/finalize)."""

    # Hand-instrumented span per [[09-operations/observability]] §
    # Tracing. The span is a no-op when tracing is disabled (env var
    # OTEL_EXPORTER_OTLP_ENDPOINT unset). Attributes are set as soon as
    # the query is validated so a trace failing on bad_query still
    # carries enough context to debug.
    with _tracer.start_as_current_span("retrieve.orchestration") as _span:
        # 1. validate query
        if isinstance(query, dict):
            try:
                parsed_query = RetrievalQuery.model_validate(query)
            except ValidationError as e:
                _span.set_attribute("musubi.query.invalid", True)
                return Err(error=RetrievalError(kind="bad_query", detail=str(e)))
        else:
            try:
                parsed_query = RetrievalQuery.model_validate(query.model_dump())
            except ValidationError as e:
                _span.set_attribute("musubi.query.invalid", True)
                return Err(error=RetrievalError(kind="bad_query", detail=str(e)))

        _span.set_attribute("musubi.namespace", parsed_query.namespace)
        _span.set_attribute("musubi.mode", parsed_query.mode)
        _span.set_attribute("musubi.limit", parsed_query.limit)

        # Basic auth check handled upstream, here we just dispatch.
        # Expand the query into per-plane pipeline runs when the router
        # supplied explicit `namespace_targets`; otherwise derive a
        # single target from the top-level namespace (legacy path —
        # orchestrator callers that don't go through the HTTP router).
        if parsed_query.namespace_targets:
            targets = [(t.namespace, t.plane) for t in parsed_query.namespace_targets]
        else:
            derived_plane = (
                parsed_query.namespace.rsplit("/", 1)[-1]
                if parsed_query.namespace.count("/") == 2
                else (parsed_query.planes[0] if parsed_query.planes else "episodic")
            )
            targets = [(parsed_query.namespace, derived_plane)]
        _span.set_attribute("musubi.target_count", len(targets))

        # Single-target fast path preserves the current behaviour bit-
        # for-bit (one pipeline run, one Qdrant query per collection,
        # no merge). Multi-target path runs the same pipeline per
        # target concurrently and merges ranked results by score at
        # the end.
        if len(targets) == 1:
            single = await _run_single(
                client=client,
                embedder=embedder,
                reranker=reranker,
                llm=llm,
                parsed_query=parsed_query,
                namespace=targets[0][0],
                plane=targets[0][1],
                now=now,
            )
            if isinstance(single, Ok):
                return Ok(
                    value=RetrievalEnvelope(
                        results=single.value.results, warnings=dedupe(single.value.warnings)
                    )
                )
            return single

        # Fan out — one pipeline run per (namespace, plane) target.
        # Call `_run_single` directly rather than recursing through
        # `retrieve()`: the query is already parsed + validated, and
        # re-entering the top-level would redo target expansion,
        # pydantic validation, and the single-vs-multi branch logic
        # for each leg. `gather(return_exceptions=True)` so a single
        # plane failing doesn't blank the whole cross-plane response
        # (ADR 0028).
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

        # Merge dedup keeps the **highest-scoring** hit per object_id.
        # First-seen dedup would drop a stronger match purely because
        # it arrived from a later target in the gather order. Build a
        # {object_id → best hit} map, then materialise once at the end.
        best_by_id: dict[str, RetrievalResult] = {}
        warnings: list[RetrievalWarning] = []
        transient_any = False
        internal_err: RetrievalError | None = None
        for (_ns, plane), outcome in zip(targets, results_per_target, strict=True):
            # Client disconnect / server shutdown produces
            # CancelledError. Re-raise so the cancellation propagates
            # up through the request lifecycle — swallowing it to
            # return a partial response would be worse than surfacing
            # the abort cleanly.
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, Err):
                # Per-plane failures degrade per-plane and surface as a bounded structured warning
                # that PRESERVES which plane failed (the old `transient_any` boolean discarded this).
                # An internal/bad_query error from *any* plane surfaces as a 5xx/4xx because the
                # merged response would silently under-report.
                if outcome.error.kind == "timeout":
                    transient_any = True
                    warnings.append(plane_timeout(plane))
                    continue
                if outcome.error.kind in ("internal", "bad_query"):
                    internal_err = outcome.error
                    break
                warnings.append(plane_error(plane))
                continue
            if isinstance(outcome, Ok):
                warnings.extend(outcome.value.warnings)
                for hit in outcome.value.results:
                    current = best_by_id.get(hit.object_id)
                    if current is None or hit.score > current.score:
                        best_by_id[hit.object_id] = hit
                continue
            if isinstance(outcome, Exception):
                logger.warning("cross-plane retrieve per-plane exception: %r", outcome)
                internal_err = RetrievalError(kind="internal", detail=str(outcome))
                break

        if internal_err is not None:
            return Err(error=internal_err)
        if not best_by_id and transient_any:
            return Err(error=RetrievalError(kind="timeout", detail="all planes timed out"))

        merged = sorted(best_by_id.values(), key=lambda r: r.score, reverse=True)
        # Dedupe warnings to distinct (code, plane) ONLY at the final request boundary.
        return Ok(
            value=RetrievalEnvelope(
                results=merged[: parsed_query.limit], warnings=dedupe(tuple(warnings))
            )
        )


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
) -> Result[RetrievalEnvelope, RetrievalError]:
    """Single-target pipeline dispatch. Extracted from :func:`retrieve`
    so cross-plane fanout can call it per target without re-parsing
    the query shape. Behaviour is identical to the pre-fanout code —
    a single call with ``planes=[plane]`` and the given namespace."""

    mode = parsed_query.mode
    warnings: list[RetrievalWarning] = []
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
                return Err(
                    error=RetrievalError(
                        kind=_kind_from_code(b_res.error.code), detail=b_res.error.detail
                    )
                )

            warnings.extend(b_res.value.warnings)
            return Ok(
                value=RetrievalEnvelope(
                    results=_pack_scored_hits(
                        b_res.value.results,
                        include_payload=not getattr(parsed_query, "brief", False),
                    ),
                    warnings=tuple(warnings),
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
                return Err(
                    error=RetrievalError(
                        kind=_kind_from_code(d_res.error.code), detail=d_res.error.detail
                    )
                )

            warnings.extend(d_res.value.warnings)
            return Ok(
                value=RetrievalEnvelope(
                    results=_pack_scored_hits(
                        d_res.value.hits,
                        include_payload=not getattr(parsed_query, "brief", False),
                    ),
                    warnings=tuple(warnings),
                )
            )

        elif mode == "recent":
            # Recent mode: pure time-ordered scroll. No embedder. No rerank.
            # Per slice-retrieve-recent. If query_text was passed, log and
            # ignore (accept-and-ignore design decision).
            if parsed_query.query_text:
                logger.warning(
                    "retrieve mode=recent ignoring query_text "
                    "(slice-retrieve-recent: accept-and-ignore at boundary)"
                )
            # Recent's default state_filter deliberately includes provisional
            # (mode purpose is "what just happened"; provisional is the
            # freshest tier). Per slice-retrieve-recent design decisions.
            recent_states: tuple[LifecycleState, ...] = (
                cast(Any, tuple(parsed_query.state_filter))
                if parsed_query.state_filter
                else ("provisional", "matured", "promoted")
            )
            r_res = await asyncio.wait_for(
                run_recent_retrieve(
                    client=client,
                    namespace=target_namespace,
                    collection="musubi_" + plane,
                    limit=parsed_query.limit,
                    since=parsed_query.since,
                    tags=parsed_query.tags,
                    state_filter=recent_states,
                ),
                # Generous vs spec's 200ms p99 budget; scroll on an indexed
                # field is fast, but Qdrant cold-cache + network jitter
                # benefit from headroom. The timeout is a safety net, not a
                # latency contract.
                timeout=2.0,
            )
            if isinstance(r_res, Err):
                map_kind: Literal["bad_query", "internal"] = (
                    "internal" if r_res.error.status_code >= 500 else "bad_query"
                )
                return Err(error=RetrievalError(kind=map_kind, detail=r_res.error.detail))

            recent_results: list[RetrievalResult] = []
            for recent_hit in r_res.value.results:
                stored_namespace = recent_hit.payload.get("namespace")
                raw_state = recent_hit.payload.get("state")
                raw_importance = recent_hit.payload.get("importance")
                # RET-003: provenance_score is exact-table-only via
                # `_provenance_score_for(plane, state)`. Returns None
                # when state is None or (plane, state) is absent from
                # the explicit lookup table. NOT `scoring._provenance`
                # (which floors unknowns to 0.1).
                recent_plane = str(recent_hit.payload.get("plane", plane))
                prov_score = _provenance_score_for(plane=recent_plane, state=raw_state)
                recent_results.append(
                    RetrievalResult(
                        object_id=recent_hit.object_id,
                        namespace=str(stored_namespace) if stored_namespace else target_namespace,
                        plane=recent_plane,
                        title=recent_hit.payload.get("title"),
                        snippet=recent_hit.snippet,
                        # Recent has no relevance scoring; row order is
                        # the signal. Score = created_epoch so cross-target
                        # merge in the fanout preserves newest-first.
                        score=recent_hit.created_epoch,
                        # RET-003 recent: `score_components` is the
                        # exact empty {} (typed `RecentScoreComponents`).
                        # Not `null`; not a fabricated 3-key dict.
                        score_components={},
                        lineage=_summarize_lineage(recent_hit.payload),
                        payload=(
                            recent_hit.payload
                            if not getattr(parsed_query, "brief", False)
                            else None
                        ),
                        state=raw_state if raw_state is not None else None,
                        importance=raw_importance if raw_importance is not None else None,
                        provenance_score=prov_score,
                    )
                )
            return Ok(value=RetrievalEnvelope(results=recent_results, warnings=tuple(warnings)))

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
                return Err(
                    error=RetrievalError(
                        kind=_kind_from_status(f_res.error.status_code), detail=f_res.error.detail
                    )
                )

            warnings.extend(f_res.value.warnings)

            results = []
            for hit in f_res.value.results:
                # Pull namespace from the row's stored payload, not the
                # request's `target_namespace`. They're equal under a strict
                # 1:1 filter (production Qdrant), but wildcard fanout
                # (ADR 0031) and any future filter-relaxation surfaces
                # rows whose stored namespace differs from the leg's
                # request — preserve provenance instead of overwriting.
                stored_namespace = hit.payload.get("namespace")
                raw_state = hit.payload.get("state")
                raw_importance = hit.payload.get("importance")
                results.append(
                    RetrievalResult(
                        object_id=hit.object_id,
                        namespace=str(stored_namespace) if stored_namespace else target_namespace,
                        plane=str(hit.payload.get("plane", "episodic")),
                        title=hit.payload.get("title"),
                        snippet=hit.snippet,
                        score=hit.score,
                        score_components={
                            "relevance": hit.score_components.relevance,
                            "recency": hit.score_components.recency,
                            "importance": hit.score_components.importance,
                            "provenance": hit.score_components.provenance,
                            "reinforcement": hit.score_components.reinforce,
                        },
                        lineage=hit.lineage_summary,
                        payload=hit.payload,
                        state=raw_state if raw_state is not None else None,
                        importance=raw_importance if raw_importance is not None else None,
                    )
                )
            return Ok(value=RetrievalEnvelope(results=results, warnings=tuple(warnings)))

        else:
            return Err(error=RetrievalError(kind="bad_query", detail=f"Unknown mode: {mode}"))

    except TimeoutError:
        return Err(error=RetrievalError(kind="timeout", detail=f"{mode} retrieval timed out"))
    except Exception as e:
        logger.error("Internal retrieval error: %s", e, exc_info=True)
        return Err(error=RetrievalError(kind="internal", detail=str(e)))


def _pack_scored_hits(hits: Sequence[Any], include_payload: bool) -> list[RetrievalResult]:
    results = []
    for hit in hits:
        # Extract source-backed `state` and `importance` BEFORE the
        # optional payload projection (per spec §6.6: present-valid
        # → exact, missing → null, present-invalid → raise). Invalid
        # values are caught at the Pydantic response layer (LifecycleState
        # literal + int 1..10); a bad enum / out-of-range value fails
        # the response validation → 500 (server integrity, NOT 422).
        payload = hit.payload
        raw_state = payload.get("state")
        raw_importance = payload.get("importance")
        results.append(
            RetrievalResult(
                object_id=hit.object_id,
                namespace=payload.get("namespace", ""),
                plane=hit.plane,
                title=payload.get("title"),
                snippet=_snippet(payload, max_chars=300),
                score=hit.score,
                score_components={
                    "relevance": hit.score_components.relevance,
                    "recency": hit.score_components.recency,
                    "importance": hit.score_components.importance,
                    "provenance": hit.score_components.provenance,
                    # Public boundary uses `reinforcement` (full word).
                    # Internal `ScoreComponents.reinforce` is unchanged.
                    "reinforcement": hit.score_components.reinforce,
                },
                lineage=_summarize_lineage(payload),
                payload=payload if include_payload else None,
                state=raw_state if raw_state is not None else None,
                importance=raw_importance if raw_importance is not None else None,
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
