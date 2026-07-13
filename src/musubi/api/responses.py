"""Shared response models used across read routers.

The plane-specific payloads are the same pydantic types from
``src/musubi/types/`` — they round-trip cleanly through FastAPI's JSON
encoder. These wrappers just document the list / collection shapes.

RET-003 ranked vs recent wire contract (slice-api-v1-ret003-wire):
  - Top-level response variants with `mode` discriminator:
    `RankedRetrieveResponse` (mode in {fast, deep, blended}) and
    `RecentRetrieveResponse` (mode=recent).
  - Typed `extra`: `RankedExtra` / `RecentExtra` with typed
    `RankedScoreComponents` (5 fields) / `RecentScoreComponents` (= {}).
  - Required-nullable `state` / `importance` (no `default=` so they
    are required in OpenAPI but may be `null` on the wire).
  - Recent has top-level `provenance_score: float | None` (exact-table-only).
  - Recent has top-level `score_kind: Literal["created_epoch"]`.
  - Ranked has top-level `score_kind: Literal["ranked_combined"]`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from musubi.types.common import LifecycleState


class HealthResponse(BaseModel):
    status: str
    version: str = "v0"


class ComponentStatus(BaseModel):
    name: str
    healthy: bool
    detail: str = ""


class StatusResponse(BaseModel):
    status: str
    version: str | None = None
    components: dict[str, ComponentStatus]


class NamespaceListResponse(BaseModel):
    items: list[str]


class NamespaceStats(BaseModel):
    namespace: str
    counts: dict[str, int]
    last_activity_epoch: float | None = None


class RetrieveResultRow(BaseModel):
    """Legacy single-shape row (kept for back-compat; new code should use
    `RankedResultRow` or `RecentResultRow`).

    RET-003 ranked vs recent wire: this legacy shape is REPLACED by
    two typed variants (`RankedResultRow`, `RecentResultRow`) with a
    `mode` discriminator at the top level. New code should NOT extend
    this row; extend the two variants.
    """

    object_id: str
    score: float
    plane: str
    content: str
    namespace: str
    title: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# RET-003 ranked vs recent wire contract
# Top-level response variants with a `mode` discriminator. Rows do NOT
# carry `mode` (per spec §4.1: "rows have no mode, so a row-level
# discriminator cannot discriminate").
# ---------------------------------------------------------------------------


class RankedScoreComponents(BaseModel):
    """The 5 real contributors exposed on the wire for ranked mode.

    All 5 fields are REQUIRED (`extra='forbid'` rejects fabrication).
    Values are clamped to [0, 1]. A ranked row missing any of the 5
    fields is a server-side data integrity failure and fails loud at
    response validation (500, NOT 422 — per spec §4.6: corrupt stored
    data is server integrity, not request validation).
    """

    model_config = ConfigDict(extra="forbid")

    relevance: Annotated[float, Field(ge=0.0, le=1.0)]
    recency: Annotated[float, Field(ge=0.0, le=1.0)]
    importance: Annotated[float, Field(ge=0.0, le=1.0)]
    provenance: Annotated[float, Field(ge=0.0, le=1.0)]
    reinforcement: Annotated[float, Field(ge=0.0, le=1.0)]


class RecentScoreComponents(BaseModel):
    """The exact empty object for recent mode.

    Recent has no breakdown: `score_components == {}` typed. Never `null`.
    `extra='forbid'` rejects any non-empty input at Pydantic validation
    time (which surfaces as 500, NOT 422 — per spec §4.3).
    """

    model_config = ConfigDict(extra="forbid")

    # No fields; serialization is exactly `{}`. Non-empty input fails
    # the forbid-extra validation.


class RankedExtra(BaseModel):
    """Typed `extra` for ranked rows: `score_components: RankedScoreComponents` plus `lineage`."""

    model_config = ConfigDict(extra="forbid")

    score_components: RankedScoreComponents
    lineage: dict[str, Any] = Field(default_factory=dict)


class RecentExtra(BaseModel):
    """Typed `extra` for recent rows: `score_components: RecentScoreComponents` (exact `{}`) plus `lineage`."""

    model_config = ConfigDict(extra="forbid")

    score_components: RecentScoreComponents = Field(default_factory=RecentScoreComponents)
    lineage: dict[str, Any] = Field(default_factory=dict)


class RankedResultRow(BaseModel):
    """One row in a ranked-mode response.

    Required fields (per spec §4.3 + Yua 2026-07-13 11:57:59 #3):
    `object_id`, `namespace`, `plane`, `score`, `content`, `state`,
    `importance`, `score_kind`, `extra`. `title` is optional.

    `state` and `importance` are REQUIRED-NULLABLE (no `default=`) so
    the OpenAPI schema lists them as required but the wire value may
    be `null` for missing-legacy rows. The orchestration layer
    populates these from the source row (per spec §6 invalid source
    semantics: present-invalid → 500, present-valid → exact, missing
    → null).
    """

    model_config = ConfigDict(extra="forbid")

    object_id: str
    namespace: str
    plane: str
    score: float
    content: str
    state: LifecycleState | None
    # importance: required-nullable (no default=) with int 1..10 when
    # present. Pydantic raises ValidationError on out-of-range values
    # (e.g. 42) at response validation → 500 (server integrity, NOT
    # 422; per spec §4.6 invalid source semantics). The
    # `Annotated[..., Field(...)]` form (no default arg) keeps the
    # field REQUIRED in OpenAPI while still allowing `null`.
    importance: Annotated[int | None, Field(ge=1, le=10)]
    score_kind: Literal["ranked_combined"]
    extra: RankedExtra
    title: str | None = None


class RecentResultRow(BaseModel):
    """One row in a recent-mode response.

    Required fields (per spec §4.3 + Yua 2026-07-13 11:57:59 #3):
    `object_id`, `namespace`, `plane`, `score`, `content`, `state`,
    `importance`, `score_kind`, `provenance_score`, `extra`. `title`
    is optional.

    `state`, `importance`, `provenance_score` are REQUIRED-NULLABLE
    (no `default=`); they are present on every row but the value may
    be `null` for missing-legacy rows.
    """

    model_config = ConfigDict(extra="forbid")

    object_id: str
    namespace: str
    plane: str
    score: float
    content: str
    state: LifecycleState | None
    # importance: required-nullable (no default=) with int 1..10 when
    # present. See `RankedResultRow.importance` for the rationale.
    importance: Annotated[int | None, Field(ge=1, le=10)]
    score_kind: Literal["created_epoch"]
    provenance_score: float | None
    extra: RecentExtra
    title: str | None = None


class _RetrieveResponseBase(BaseModel):
    """Shared base for the two top-level response variants."""

    results: list[RankedResultRow | RecentResultRow]
    limit: int
    #: RET-007 — additive, default-empty. Bounded degradation codes
    #: surfaced from a degraded 200 so clients can tell degraded from
    #: healthy. A healthy response carries ``[]``.
    warnings: list[str] = Field(default_factory=list)


class RankedRetrieveResponse(_RetrieveResponseBase):
    """Top-level response for mode in {fast, deep, blended}.

    `mode` is the top-level discriminator (rows do NOT carry `mode`).
    FastAPI emits this as one of the two variants in the response's
    oneOf at `/v1/openapi.json`.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fast", "deep", "blended"]


class RecentRetrieveResponse(_RetrieveResponseBase):
    """Top-level response for mode='recent'."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["recent"]


# Public alias for the discriminated union. We use Pydantic's
# `Field(discriminator=...)` to emit an explicit OpenAPI discriminator
# (FastAPI translates to `oneOf` + `discriminator: {propertyName: mode,
# mapping: {...}}` on the response schema). Without the discriminator,
# FastAPI emits `anyOf` (no discriminator mapping) which is functionally
# equivalent for non-overlapping Literal-typed variants but lacks the
# client-side dispatch hint. The discriminator is the locked contract
# per spec §4.1 and Yua 2026-07-13 11:57:59 #4.
RetrieveResponse = Annotated[
    RankedRetrieveResponse | RecentRetrieveResponse,
    Field(discriminator="mode"),
]


class ContradictionPair(BaseModel):
    object_id: str
    contradicts: list[str]
    namespace: str


class ContradictionListResponse(BaseModel):
    items: list[ContradictionPair]


class LifecycleEventRow(BaseModel):
    event_id: str
    object_id: str
    object_type: str
    namespace: str
    from_state: str
    to_state: str
    actor: str
    reason: str
    occurred_epoch: float


class LifecycleEventListResponse(BaseModel):
    items: list[LifecycleEventRow]


class ThoughtListResponse(BaseModel):
    items: list[dict[str, Any]]


__all__ = [
    "ComponentStatus",
    "ContradictionListResponse",
    "ContradictionPair",
    "HealthResponse",
    "LifecycleEventListResponse",
    "LifecycleEventRow",
    "NamespaceListResponse",
    "NamespaceStats",
    "RankedExtra",
    "RankedResultRow",
    "RankedRetrieveResponse",
    "RankedScoreComponents",
    "RecentExtra",
    "RecentResultRow",
    "RecentRetrieveResponse",
    "RecentScoreComponents",
    "RetrieveResponse",
    "RetrieveResultRow",
    "StatusResponse",
    "ThoughtListResponse",
]
