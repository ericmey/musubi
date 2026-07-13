"""Retrieval read endpoint.

POST /v1/retrieve is a read in disguise — body carries the query
parameters; no state mutation. NDJSON streaming variant
(POST /v1/retrieve/stream) lives in ``writes_retrieve_stream.py``.

Three namespace shapes are accepted:

- **3-segment** (``tenant/presence/plane``): single-plane query. The
  stored-row filter is literal; the ``planes`` field, if set, must
  not contradict the namespace's trailing plane.
- **2-segment** (``tenant/presence``): cross-plane query. Each entry
  in ``planes`` is expanded to ``<namespace>/<plane>`` server-side
  and the pipeline fans out, merging results by score. Scope is
  checked **strictly per plane** — a token requesting any plane it
  can't read 403s the entire request rather than silently omitting
  that plane (ADR 0028).
- **Wildcard segments** (per ADR 0031): ``*`` matches any single
  segment. ``nyla/*/episodic`` fans an episodic retrieve across all
  of Nyla's channels; ``*/voice/curated`` spans every agent's voice
  curated. Wildcards are expanded server-side against the live Qdrant
  payload, then the resolved concrete targets feed the same fanout
  pipeline above. Strict scope still applies — every expanded target
  must be readable by the token. Writes still reject ``*``.

Dispatches to :func:`musubi.retrieve.orchestration.retrieve`, which
runs the per-mode pipeline (``fast`` → vector + recency + reinforcement
scoring; ``deep`` → full hybrid + cross-encoder rerank + lineage
hydration; ``blended`` → hybrid without the reranker). The router
does auth + body validation + shape expansion + error mapping;
everything interesting happens behind the orchestration boundary.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, model_validator
from qdrant_client import QdrantClient

from musubi.api.dependencies import (
    get_embedder,
    get_qdrant_client,
    get_reranker,
    get_settings_dep,
)
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import RetrieveResponse, RetrieveResultRow
from musubi.auth import authenticate_request
from musubi.auth.scopes import resolve_namespace_scope
from musubi.embedding import Embedder, TEIRerankerClient
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.settings import Settings
from musubi.store import collection_for_plane
from musubi.types.common import Err

router = APIRouter(prefix="/v1/retrieve", tags=["retrieve"])


class RetrieveQuery(BaseModel):
    namespace: str = Field(
        ...,
        description=(
            "Namespace pattern. Three shapes accepted: "
            "3-segment concrete `<tenant>/<presence>/<plane>` (single target), "
            "2-segment `<tenant>/<presence>` (cross-plane fanout, requires `planes`), "
            "or wildcard with `*` replacing any single segment "
            "(e.g. `nyla/*/episodic`, `*/voice/curated`). "
            "Writes reject `*`; wildcards are read-only. See ADR 0031."
        ),
    )
    # query_text required for fast/deep/blended; optional for recent.
    # Enforced by the router-side model_validator below so the wire-level
    # response stays at 422 (FastAPI body validation) for ranked-mode
    # callers that omit query_text — preserves the pre-slice contract
    # rather than turning it into a 400 from the orchestration layer.
    query_text: str = ""
    # `Literal` matches the `enum` constraint in openapi.yaml. Without
    # this, the pydantic body model would accept any string while the
    # published schema told clients only four values were valid; pydantic
    # would silently pass through and orchestration would emit a 400
    # `bad_query`. Keeping the two surfaces in sync produces a 422 here
    # for unknown values, matching every other ranked-mode validation.
    mode: Literal["fast", "deep", "blended", "recent"] = "fast"
    limit: int = 10
    planes: list[str] | None = None
    include_archived: bool = False
    #: Inclusive epoch-seconds floor — only consumed by ``mode="recent"``.
    #: Ignored by other modes (which rank with their own recency-weighting).
    since: float | None = Field(
        default=None,
        description=(
            "Recent-mode only: inclusive lower bound on `created_epoch`. "
            "Rows older than `since` are excluded. ISO-format timestamps "
            "are NOT accepted — convert client-side via "
            "`datetime.timestamp()`."
        ),
    )
    #: Tag-AND filter — a row must contain every listed tag to match.
    #: Currently consumed only by ``mode="recent"``; accepted by other
    #: modes without effect (forward-compat).
    tags: list[str] | None = Field(
        default=None,
        description=(
            "Recent-mode tag filter. AND semantics — a row must contain "
            "every listed tag to match. Empty list and `null` both mean "
            "no filter."
        ),
    )
    state_filter: list[str] | None = Field(
        default=None,
        description=(
            "Lifecycle states to include. Default `null` resolves to "
            "`('matured', 'promoted')` for fast/deep/blended (matching the "
            "pre-`recent` behaviour). For `mode='recent'`, the default is "
            "`('provisional', 'matured', 'promoted')` — recent's purpose "
            "is 'what just happened', so the freshest tier is included by "
            "default. Set explicitly to `['provisional', 'matured', "
            "'promoted']` for recall on ranked modes when you want fresh "
            "deliberate `memory_store` rows visible before they age "
            "through the maturation cron. "
            "Note: in `mode='fast'`, `include_archived: true` augments "
            "the default by adding `('demoted', 'archived', 'superseded')`. "
            "In `mode='deep'` and `mode='blended'`, `include_archived` is "
            "currently ignored — pass `state_filter` explicitly when those "
            "modes need archive-side states."
        ),
    )

    @model_validator(mode="after")
    def _require_query_text_for_ranked_modes(self) -> RetrieveQuery:
        """Match the orchestration-side rule at the wire boundary.

        Pre-slice, `query_text` was a required `str` field and FastAPI
        body validation rejected a missing one with **422**. Now that
        `query_text` defaults to `""` (so `mode="recent"` can omit it),
        a ranked-mode caller missing `query_text` would be caught by
        the orchestration validator and surface as a **400** —
        a wire-level status-code change for existing callers.

        Enforcing the same cross-field rule here keeps the contract at
        422 for ranked-mode bodies missing `query_text`, matching every
        other body-validation error from FastAPI.
        """
        if self.mode != "recent" and not self.query_text.strip():
            raise ValueError(
                f"query_text is required for mode={self.mode!r} (only mode='recent' may omit it)"
            )
        return self


# orchestration.RetrievalError.kind → (HTTP status, typed error code).
# `timeout` maps to BACKEND_UNAVAILABLE because a timeout in orchestration
# means Qdrant or TEI didn't respond within budget — same shape as any
# upstream outage from the caller's perspective.
_KIND_STATUS_MAP: dict[str, tuple[int, ErrorCode]] = {
    "bad_query": (400, "BAD_REQUEST"),
    "forbidden": (403, "FORBIDDEN"),
    "timeout": (503, "BACKEND_UNAVAILABLE"),
    "internal": (500, "INTERNAL"),
}


_VALID_PLANES: frozenset[str] = frozenset({"episodic", "curated", "concept", "artifact"})


def _namespace_shape(namespace: str) -> int:
    """Number of ``/``-separated segments in ``namespace``."""
    return len(namespace.split("/"))


def _segment_is_valid(seg: str) -> bool:
    """A namespace segment is either exactly ``*`` (wildcard) or contains
    no ``*`` at all (literal). Mixed forms (``**``, ``n*``, ``*foo``) are
    rejected so `*` stays a whole-segment primitive — never a regex char.
    Empty segments are caught separately upstream."""
    if seg == "*":
        return True
    return "*" not in seg


def _dedup_planes(planes: list[str]) -> list[str]:
    """Dedup a planes list in first-seen order. ``["episodic", "episodic"]``
    is either a typo or retry shape; either way running the pipeline twice
    for one target wastes work and skews merge ordering."""
    seen: set[str] = set()
    out: list[str] = []
    for plane in planes:
        if plane in seen:
            continue
        seen.add(plane)
        out.append(plane)
    return out


def _resolve_targets(
    namespace: str,
    planes: list[str] | None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Expand a retrieve body into ``(namespace, plane)`` targets, possibly
    still containing ``*`` segments.

    Returns ``(targets, error)``. ``error`` is a string describing a
    shape problem (unknown plane, 3-seg/planes mismatch, malformed
    segment); ``targets`` is empty in that case. A valid expansion always
    produces at least one target.

    - 3-segment namespace: one target if the trailing plane is concrete;
      ``*`` plane requires a ``planes`` list and emits one target per
      requested plane (sugar over the 2-seg shape).
    - 2-segment namespace: one target per entry in ``planes``. If
      ``planes`` is unset, default to ``["episodic"]`` to match the
      pre-fanout behaviour. ``*`` is allowed in either segment; expansion
      against Qdrant happens later in :func:`_expand_wildcard_targets`.
    """
    shape = _namespace_shape(namespace)
    requested = list(planes) if planes else None
    segments = namespace.split("/")

    # Reject empty segments up front so `a/b/` doesn't slip through
    # as a "3-segment" with trailing empty plane.
    if any(seg == "" for seg in segments):
        return ([], f"namespace '{namespace}' has empty segments")

    for seg in segments:
        if not _segment_is_valid(seg):
            return (
                [],
                f"namespace '{namespace}' has invalid segment '{seg}': "
                "segments must be either '*' or a literal identifier "
                "(no mixed-wildcard forms like '**' or 'n*')",
            )

    if shape == 3:
        derived_plane = segments[-1]
        if derived_plane == "*":
            # Wildcard plane segment behaves like 2-seg + planes list.
            if requested is None:
                return (
                    [],
                    f"3-segment namespace '{namespace}' has '*' plane "
                    "segment; a 'planes' list is required to expand it",
                )
            deduped = _dedup_planes(requested)
            for plane in deduped:
                if plane not in _VALID_PLANES:
                    return (
                        [],
                        f"unknown plane '{plane}' in planes list (valid: {sorted(_VALID_PLANES)})",
                    )
            base = "/".join(segments[:-1])
            return ([(f"{base}/{plane}", plane) for plane in deduped], None)
        if derived_plane not in _VALID_PLANES:
            return (
                [],
                f"3-segment namespace '{namespace}' names unknown plane "
                f"'{derived_plane}' (valid: {sorted(_VALID_PLANES)})",
            )
        if requested is not None and requested != [derived_plane]:
            return (
                [],
                f"3-segment namespace '{namespace}' pins plane "
                f"'{derived_plane}'; planes={requested} is inconsistent",
            )
        return ([(namespace, derived_plane)], None)

    if shape == 2:
        target_planes = requested if requested is not None else ["episodic"]
        deduped = _dedup_planes(target_planes)
        for plane in deduped:
            if plane not in _VALID_PLANES:
                return (
                    [],
                    f"unknown plane '{plane}' in planes list (valid: {sorted(_VALID_PLANES)})",
                )
        return ([(f"{namespace}/{plane}", plane) for plane in deduped], None)

    return ([], f"namespace '{namespace}' must be 2- or 3-segment")


def _segments_match(pattern_segs: list[str], stored_segs: list[str]) -> bool:
    """True if ``stored_segs`` segment-matches ``pattern_segs``. ``*`` in
    a pattern segment matches any literal in that position; literal
    segments must be exactly equal. Segment counts must match."""
    if len(pattern_segs) != len(stored_segs):
        return False
    for ps, ss in zip(pattern_segs, stored_segs, strict=True):
        if ps == "*":
            continue
        if ps != ss:
            return False
    return True


def _expand_wildcard_targets(
    client: QdrantClient,
    targets: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Expand any ``*``-bearing target by enumerating concrete matches
    from Qdrant.

    For each ``(namespace, plane)`` target:

    - If ``namespace`` contains no ``*``, pass through unchanged.
    - Otherwise scroll the plane's collection (payload-only ``namespace``),
      dedup, segment-match against the pattern, and emit one target per
      matched stored namespace.

    Order: targets retain their relative input order; within an expanded
    pattern, results are sorted lexicographically for determinism.

    No cache (per ADR 0031). Empty matches yield no targets — the empty
    result is signalled by the absence of any (namespace, plane) tuple
    for that pattern.
    """
    expanded: list[tuple[str, str]] = []
    for namespace, plane in targets:
        if "*" not in namespace:
            expanded.append((namespace, plane))
            continue
        pattern_segs = namespace.split("/")
        seen: set[str] = set()
        collection = collection_for_plane(plane)
        next_offset: int | str | None = None
        while True:
            points, next_offset = client.scroll(  # type: ignore[assignment]
                collection_name=collection,
                with_payload=["namespace"],
                with_vectors=False,
                limit=1000,
                offset=next_offset,
            )
            for point in points:
                payload = point.payload or {}
                ns = payload.get("namespace")
                if not isinstance(ns, str) or ns in seen:
                    continue
                if _segments_match(pattern_segs, ns.split("/")):
                    seen.add(ns)
            if next_offset is None:
                break
        for ns in sorted(seen):
            expanded.append((ns, plane))
    return expanded


@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: Request,
    body: RetrieveQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
    reranker: TEIRerankerClient = Depends(get_reranker),
) -> RetrieveResponse:
    targets, shape_err = _resolve_targets(body.namespace, body.planes)
    if shape_err is not None:
        raise APIError(status_code=400, code="BAD_REQUEST", detail=shape_err)

    # Authenticate first so unauth callers cannot probe for empty
    # wildcard matches. Token check is once per request — the per-target
    # work is scope evaluation on the single resulting context.
    auth_result = authenticate_request(
        request,  # type: ignore[arg-type]
        None,
        settings=settings,
    )
    if isinstance(auth_result, Err):
        err = auth_result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(status_code=err.status_code, code=code, detail=err.detail)
    context = auth_result.value

    # Wildcard segments resolve against live Qdrant payload. An empty
    # expansion of a wildcard pattern is a valid state — no rows in any
    # matching channel yet — so short-circuit to empty results. Concrete
    # (no-`*`) namespaces still run the full pipeline; only wildcard
    # patterns get the early-return treatment.
    pattern_had_wildcards = any("*" in ns for ns, _ in targets)
    targets = _expand_wildcard_targets(qdrant, targets)
    if pattern_had_wildcards and not targets:
        return RetrieveResponse(results=[], mode=body.mode, limit=body.limit)

    for target_namespace, _plane in targets:
        scope_result = resolve_namespace_scope(context, namespace=target_namespace, access="r")
        if isinstance(scope_result, Err):
            raise APIError(
                status_code=scope_result.error.status_code,
                code="FORBIDDEN",
                detail=scope_result.error.detail,
            )

    # Hand orchestration the fully-resolved targets. A 3-segment
    # call reduces to exactly one (namespace, plane) target, so the
    # single-plane code path is preserved bit-for-bit.
    query_body: dict[str, object] = {
        "namespace": body.namespace,
        "query_text": body.query_text,
        "mode": body.mode,
        "limit": body.limit,
        "planes": [plane for _, plane in targets],
        "include_archived": body.include_archived,
        "namespace_targets": [{"namespace": ns, "plane": plane} for ns, plane in targets],
    }
    if body.state_filter is not None:
        query_body["state_filter"] = body.state_filter
    if body.since is not None:
        query_body["since"] = body.since
    if body.tags is not None:
        query_body["tags"] = body.tags

    orchestration_result = await run_orchestration_retrieve(
        client=qdrant,
        embedder=embedder,
        reranker=reranker,
        query=query_body,
    )

    if isinstance(orchestration_result, Err):
        retrieval_err = orchestration_result.error
        # Telemetry (errors_total) is counted at the shared orchestration boundary — not here.
        status, error_code = _KIND_STATUS_MAP.get(retrieval_err.kind, (500, "INTERNAL"))
        raise APIError(status_code=status, code=error_code, detail=retrieval_err.detail)

    # The envelope's warnings are already deduped, fail-closed (allowlisted only), and counted at the
    # orchestration boundary — the router just flattens the bounded codes onto the wire.
    envelope = orchestration_result.value
    rows: list[RetrieveResultRow] = []
    for hit in envelope.results:
        rows.append(
            RetrieveResultRow(
                object_id=hit.object_id,
                score=hit.score,
                plane=hit.plane,
                content=hit.snippet,
                namespace=hit.namespace,
                # `title` is top-level for curated/concept/artifact (None
                # for episodic — no stable title field on that plane).
                # Consumers with a UI shouldn't have to reach into `extra`
                # for a universal display field.
                title=hit.title,
                extra={
                    "score_components": hit.score_components,
                    "lineage": hit.lineage,
                },
            )
        )

    return RetrieveResponse(
        results=rows[: body.limit],
        mode=body.mode,
        limit=body.limit,
        warnings=[warning.code for warning in envelope.warnings],
    )


__all__ = ["router"]
