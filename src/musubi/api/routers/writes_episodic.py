"""Episodic write endpoints — POST capture / batch / PATCH / DELETE."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_episodic_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.patch_guard import assert_readable_after_patch, reject_unknown_fields
from musubi.auth import AuthRequirement, authenticate_request
from musubi.lifecycle.transitions import transition
from musubi.planes.episodic import EpisodicPlane
from musubi.retrieve.context_pack import VALID_KINDS, VALID_STALENESS
from musubi.settings import Settings
from musubi.types.common import Err, Ok, utc_now
from musubi.types.episodic import EpisodicMemory


def _build_episodic(**kwargs: object) -> EpisodicMemory:
    """Construct an :class:`EpisodicMemory` from request-supplied values.

    A direct ``EpisodicMemory(**kwargs)`` call raises
    :class:`pydantic.ValidationError` on a bad namespace / importance /
    tags value. Since this is request-driven data we translate that to
    a 422 BAD_REQUEST — the same status FastAPI emits when the body
    itself fails schema validation. See the note in ``app.py`` about why
    we don't install a global handler for ``ValidationError``."""
    try:
        return EpisodicMemory(**kwargs)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise APIError(
            status_code=422,
            code="BAD_REQUEST",
            detail=str(exc),
            hint="check the request body against the OpenAPI spec",
        ) from exc


def _check_body_scope(request: Request, namespace: str, settings: Settings) -> None:
    """Validate the bearer's scope grants ``w`` on a body-supplied namespace."""
    requirement = AuthRequirement(namespace=namespace, access="w")
    result = authenticate_request(
        request,  # type: ignore[arg-type]
        requirement,
        settings=settings,
    )
    if isinstance(result, Err):
        err = result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(status_code=err.status_code, code=code, detail=err.detail)


def _require_operator_for_created_at(request: Request) -> None:
    """Guard the ``created_at`` override on capture endpoints.

    Overriding the created_at timestamp is an operator-only privilege:
    it lets the migration path preserve source-truth timestamps when
    ingesting historical data, but it must not be available to
    regular consumers because it would let a token rewrite when an
    event "happened". The bearer's ``AuthContext`` is attached to
    ``request.state.auth`` by ``_check_body_scope`` (which calls
    ``authenticate_request``) — the capture handlers don't use the
    ``require_auth`` dependency because scope is body-derived, not
    static. If the scope list doesn't include ``operator`` we 403
    before touching the plane."""
    ctx = getattr(request.state, "auth", None)
    if ctx is None or "operator" not in (ctx.scopes or ()):
        raise APIError(
            status_code=403,
            code="FORBIDDEN",
            detail=(
                "created_at override requires operator scope; pass an "
                "operator token or omit the field"
            ),
        )


router = APIRouter(prefix="/v1/episodic", tags=["episodic-writes"])


def _require_tz_aware(value: datetime | None) -> datetime | None:
    """Reject naive datetimes at the request-model layer.

    ``EpisodicMemory`` forbids ``tzinfo=None`` (``ensure_utc`` in
    types.episodic) — without this validator a naive ISO-8601 string
    would parse into the request model and then blow up at plane
    construction as a pydantic ``ValidationError``. Catching it here
    produces a clean 422 with a targeted message instead."""
    if value is not None and value.tzinfo is None:
        raise ValueError("created_at must be timezone-aware (ISO-8601 with offset or 'Z')")
    return value


def _reject_future_created_at(value: datetime | None) -> None:
    """Reject a ``created_at`` that sits in the future at request time.

    The plane also guards this (``plane.create`` raises ``ValueError``
    on the preserve path), but catching it at the API layer means the
    client gets a clean 422 instead of relying on a generic 5xx
    catchall. Both guards exist on purpose — belt-and-braces against
    somebody calling the plane directly."""
    if value is not None and value > utc_now():
        raise APIError(
            status_code=422,
            code="BAD_REQUEST",
            detail="created_at cannot be in the future",
            hint="supply a past or present timestamp, or omit the field",
        )


def _validate_context_tags(tags: list[str]) -> list[str]:
    for tag in tags:
        if tag.startswith("kind:") and tag.removeprefix("kind:") not in VALID_KINDS:
            raise ValueError(f"unknown essence kind tag {tag!r}")
        if tag.startswith("staleness:") and tag.removeprefix("staleness:") not in VALID_STALENESS:
            raise ValueError(f"unknown essence staleness tag {tag!r}")
    return tags


def _with_default_episode_tags(tags: list[str]) -> list[str]:
    out = list(tags)
    if not any(tag.startswith("kind:") for tag in out):
        out.append("kind:episode")
    if not any(tag.startswith("staleness:") for tag in out):
        out.append("staleness:episodic")
    return out


class CaptureRequest(BaseModel):
    namespace: str
    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    # Optional migration / replay override — operator scope required (see
    # _require_operator_for_created_at below). Normal consumers omit this
    # and Musubi stamps created_at at ingest time via EpisodicMemory's
    # default factory.
    created_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _tz_aware_created_at(cls, v: datetime | None) -> datetime | None:
        return _require_tz_aware(v)

    @field_validator("tags")
    @classmethod
    def _valid_context_tags(cls, v: list[str]) -> list[str]:
        return _validate_context_tags(v)


class CaptureResponse(BaseModel):
    object_id: str
    state: str
    dedup: dict[str, str] | None = None


class CaptureItem(BaseModel):
    """One row in a batch capture. Inherits ``namespace`` from the parent."""

    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    # Per-item created_at override — operator scope required (checked
    # once on the outer batch before iterating).
    created_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _tz_aware_created_at(cls, v: datetime | None) -> datetime | None:
        return _require_tz_aware(v)

    @field_validator("tags")
    @classmethod
    def _valid_context_tags(cls, v: list[str]) -> list[str]:
        return _validate_context_tags(v)


class BatchCaptureRequest(BaseModel):
    namespace: str
    items: list[CaptureItem]


class BatchCaptureResponse(BaseModel):
    object_ids: list[str]


class PatchEpisodicRequest(BaseModel):
    """Non-state field updates only. ``state`` mutations go through
    POST /v1/lifecycle/transition (the canonical primitive). Extra
    fields are captured (``extra="allow"``) so the handler can return
    a typed BAD_REQUEST naming the forbidden field instead of a
    generic 422.

    **This model IS the public PATCH contract** — the handler's allowlist is
    derived from it (``_PATCHABLE_FIELDS``), so a field that is not declared here
    is not patchable, full stop.

    ``content`` is declared because retraction depends on it. Musubi is
    append-only: a false memory cannot be deleted, it can only be rewritten to say
    that it lied. ``memory-data musubi retract`` therefore PATCHes ``content`` (plus
    summary/tags/importance), and it is the fleet's only mechanism for neutralising
    a falsehood. An earlier revision of this allowlist omitted ``content`` and would
    have returned 400 to every retraction — shipping a memory-integrity fix that
    disabled the tool for fixing memory. Caught by Yua in review of PR #398, and the
    reason this docstring now spells the contract out instead of leaving it implied.

    Note on vectors: ``content`` is patched via ``set_payload``, which does NOT
    re-embed. A retracted row keeps the embedding of its original text — which is
    the behaviour we want: searching the false claim still surfaces the row, and the
    row now says RETRACTED. If that ever changes, retraction stops being findable by
    the thing people actually remember.
    """

    model_config = ConfigDict(extra="allow")

    tags: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    summary: str | None = None
    content: str | None = Field(default=None, min_length=1)

    @field_validator("tags")
    @classmethod
    def _valid_context_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_context_tags(v)


_FORBIDDEN_PATCH_FIELDS = {"state", "version", "object_id", "namespace"}

# Derived from the model, never hand-maintained — a hand-kept list is the next
# thing to drift out of sync with what the model actually declares. Add a field to
# PatchEpisodicRequest and it becomes patchable; there is no second place to update.
_PATCHABLE_FIELDS = set(PatchEpisodicRequest.model_fields)


@router.post(
    "",
    response_model=CaptureResponse,
    status_code=202,
    operation_id="capture_episodic.bucket=capture",
)
async def capture(
    request: Request,
    body: CaptureRequest = Body(...),
    plane: EpisodicPlane = Depends(get_episodic_plane),
    settings: Settings = Depends(get_settings_dep),
) -> CaptureResponse:
    _check_body_scope(request, body.namespace, settings)
    memory_kwargs: dict[str, object] = {
        "namespace": body.namespace,
        "content": body.content,
        "summary": body.summary,
        "tags": _with_default_episode_tags(body.tags),
        "importance": body.importance,
    }
    preserve_created_at = False
    if body.created_at is not None:
        _require_operator_for_created_at(request)
        _reject_future_created_at(body.created_at)
        memory_kwargs["created_at"] = body.created_at
        preserve_created_at = True
    memory = _build_episodic(**memory_kwargs)
    saved = await plane.create(memory, preserve_created_at=preserve_created_at)
    response = CaptureResponse(object_id=saved.object_id, state=saved.state)
    request.state.idempotency_response = response.model_dump()
    return response


@router.post(
    "/batch",
    response_model=BatchCaptureResponse,
    status_code=202,
    operation_id="batch_capture.bucket=batch-write",
)
async def batch_capture(
    request: Request,
    body: BatchCaptureRequest = Body(...),
    plane: EpisodicPlane = Depends(get_episodic_plane),
    settings: Settings = Depends(get_settings_dep),
) -> BatchCaptureResponse:
    _check_body_scope(request, body.namespace, settings)
    # Check operator scope once up front if ANY item overrides
    # created_at. A batch with mixed override / no-override is fine
    # under operator scope; a batch with any override under a
    # non-operator token is a 403 for the whole batch (simpler to
    # reason about than per-item partial failures).
    if any(item.created_at is not None for item in body.items):
        _require_operator_for_created_at(request)
        for item in body.items:
            _reject_future_created_at(item.created_at)
    out: list[str] = []
    for item in body.items:
        memory_kwargs: dict[str, object] = {
            "namespace": body.namespace,
            "content": item.content,
            "summary": item.summary,
            "tags": _with_default_episode_tags(item.tags),
            "importance": item.importance,
        }
        preserve = False
        if item.created_at is not None:
            memory_kwargs["created_at"] = item.created_at
            preserve = True
        memory = _build_episodic(**memory_kwargs)
        saved = await plane.create(memory, preserve_created_at=preserve)
        out.append(saved.object_id)
    return BatchCaptureResponse(object_ids=out)


@router.patch(
    "/{object_id}",
    response_model=EpisodicMemory,
    operation_id="patch_episodic.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def patch_episodic(
    object_id: str,
    namespace: str = Query(...),
    body: PatchEpisodicRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: EpisodicPlane = Depends(get_episodic_plane),
) -> EpisodicMemory:
    """Update non-state metadata on an existing episodic row.

    State changes are forbidden on this surface — they go through
    POST /v1/lifecycle/transition so the lifecycle ledger records every
    state mutation. Tags / importance / summary are non-state metadata
    and land via a payload-only ``set_payload`` (analogous to the
    enrichment writes the maturation sweep does — see
    :mod:`musubi.lifecycle.maturation` for precedent).
    """
    # `exclude_unset`, NOT `exclude_none`.
    #
    # `exclude_none=True` drops explicitly-supplied nulls BEFORE the allowlist and the
    # canonical merged-row guard ever see them. So `PATCH {"retracted_original": null}`
    # became `{}` — the allowlist saw no unknown key, nothing was written, and the endpoint
    # returned **200 OK**. A caller who sent an unknown field was told it succeeded.
    #
    # That is a FALSE SUCCESS: the handler reported success without applying the mutation and
    # without rejecting it — the exact defect this PR exists to remove, living inside the
    # guard written to prevent it. It also conflated "field omitted" with "field explicitly
    # set to null", which are different requests. (Yua, review of d5c7e0f.)
    #
    # `exclude_unset=True` preserves the caller's ACTUAL key set, so:
    #   - unknown keys are rejected whatever their value, null included;
    #   - known nulls are judged by the canonical persisted model, not silently discarded;
    #   - omitted fields stay omitted.
    incoming = body.model_dump(exclude_unset=True)
    overlap = _FORBIDDEN_PATCH_FIELDS & set(incoming)
    if overlap:
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"PATCH cannot modify state-managed fields: {sorted(overlap)}; "
            f"use POST /v1/lifecycle/transition for state changes",
        )
    # Anything not in the model is rejected — an ALLOWLIST, not a denylist.
    #
    # This body is `extra="allow"` (so we can 400 nicely instead of 422), and the
    # payload below is written verbatim with `set_payload`. Until 2026-07-11 the only
    # gate was `_FORBIDDEN_PATCH_FIELDS` — a denylist of four names. Every key nobody
    # had thought of went straight into the Qdrant payload, where the READ model
    # (`extra="forbid"`) then rejected it *forever*: the row 500s on every subsequent
    # GET, and could not even be deleted, because the delete path's 404-guard was
    # itself a `get()`.
    #
    # The write model must never accept what the read model forbids. A denylist
    # guarding a strict reader is unsound by construction — it can only block the
    # mistakes someone already imagined.
    #
    # Lived, not theorised: on 2026-07-10 a `retracted_original` key sent through this
    # endpoint permanently bricked aoi/command-chair/episodic/3GJhJLAvYXzIp8Qe8tuPHR9S9th.
    # Note the shape of that failure — `set_payload` SUCCEEDS, then the refresh `get()`
    # raises, so the caller sees a 500 and believes the write failed while the row has
    # already been destroyed.
    reject_unknown_fields(incoming, _PATCHABLE_FIELDS, plane="episodic")

    # Read RAW so that READING does not itself blow up on an already-corrupted row.
    # `raw_payload()` returns None only when the point is absent, which is the 404 below.
    #
    # NOT a repair path: `assert_readable_after_patch()` will still refuse a patch whose
    # merged result is unreadable, and a row that already carries an unknown key stays
    # unreadable no matter what we patch — PATCH cannot REMOVE a key. Repair is a hard
    # delete or a separate raw operator path. (Copilot reviewer, PR #398.)
    current_raw = await plane.raw_payload(namespace=namespace, object_id=object_id)
    if current_raw is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
        )

    # NEVER PERSIST WHAT YOU CANNOT READ BACK.
    #
    # The allowlist above stops unknown KEYS. It does nothing about invalid VALUES of
    # known keys — `{"content": ""}` passed it, persisted, and then failed the refresh
    # read with `string_too_short`, bricking the row exactly as an unknown key did. The
    # fix for one bricking bug had introduced another.
    #
    # So we simulate the write and read it back before touching disk. This makes the
    # invariant total instead of remembered: any divergence between this request model
    # and EpisodicMemory — today's or a future one nobody has thought of — becomes a
    # clean 400 rather than a 500 and a dead memory.
    assert_readable_after_patch(current_raw, incoming, EpisodicMemory, object_id=object_id)

    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload=incoming,
        points=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
            ]
        ),
    )
    refreshed = await plane.get(namespace=namespace, object_id=object_id)
    assert refreshed is not None
    return refreshed


@router.delete(
    "/{object_id}",
    operation_id="delete_episodic.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def delete_episodic(
    request: Request,
    object_id: str,
    namespace: str = Query(...),
    hard: bool = Query(False),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: EpisodicPlane = Depends(get_episodic_plane),
) -> Response:
    """Soft-delete by default (state → archived via the canonical
    ``transition()`` primitive). ``?hard=true`` requires operator
    scope and removes the point from Qdrant entirely.

    The operator-scope check on the hard path reads the
    :class:`AuthContext` that the outer ``require_auth`` dependency has
    already attached to ``request.state.auth``.

    Neither path deserializes the row. ``get()`` model-validates, so a row with an
    unmodeled payload key raised a 500 *inside the 404-guard*, before the delete ran —
    the row was unremovable precisely because it was broken, which is exactly backwards.

    **The hard path delegates to ``plane.delete()``.** It used to do its own
    ``plane.exists()`` check and then delete via an ``object_id`` PAYLOAD FILTER — both
    of which locate the row by payload fields. So a row that had lost or malformed its
    ``namespace`` / ``object_id`` keys returned 404 and stayed stored, **through the very
    route the fleet and operators actually use.** Rev3 hardened the SDK method with
    deterministic point-ID addressing and left this route on the old contract: the path
    nobody calls was fixed, and the path that actually failed in production was not.
    (Yua, rev3 review of PR #398.)

    ``plane.delete()`` addresses the point by its deterministic ID, enforces the stored
    namespace when the payload can state one, normalizes an unreadable prior state for the
    audit record, and emits the LifecycleEvent. One hardened contract, one place.

    The soft path remains payload-filtered by design: archiving requires constructing a
    valid lifecycle transition, which an identity-damaged row cannot support. Such a row
    is removable (hard delete), not archivable — and removable is what matters."""
    if hard:
        ctx = getattr(request.state, "auth", None)
        if ctx is None or "operator" not in (ctx.scopes or ()):
            raise APIError(
                status_code=403,
                code="FORBIDDEN",
                detail="hard delete requires operator scope; pass an operator token",
            )
        try:
            await plane.delete(
                namespace=namespace,
                object_id=object_id,
                actor="api-hard-delete",
                reason="api-hard-delete",
                is_operator=True,
            )
        except LookupError as exc:
            raise APIError(
                status_code=404,
                code="NOT_FOUND",
                detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
            ) from exc
        return Response(status_code=204)
    if not await plane.exists(namespace=namespace, object_id=object_id):
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
        )
    result = transition(
        qdrant,
        object_id=object_id,
        target_state="archived",
        actor="api-delete",
        reason="api-soft-delete",
    )
    if not isinstance(result, Ok):
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"delete transition rejected: {result.error.message}",
        )
    return Response(
        status_code=200, content=b'{"status":"archived"}', media_type="application/json"
    )


__all__ = ["router"]
