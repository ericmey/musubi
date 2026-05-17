"""Base classes: ``MusubiObject`` and ``MemoryObject``.

Per [[04-data-model/object-hierarchy]], every object that exists in Musubi is a
``MusubiObject``. Objects that carry content + lineage are ``MemoryObject``s.

``MemoryObject`` is abstract in the domain sense — nothing should instantiate it
directly; use the concrete subclasses in the sibling modules.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from musubi.types.common import (
    KSUID,
    SCHEMA_VERSION,
    ArtifactRef,
    LifecycleState,
    Namespace,
    ensure_utc,
    epoch_of,
    family_of,
    generate_ksuid,
    utc_now,
)


class MusubiObject(BaseModel):
    """Base payload shape for everything persisted in Musubi.

    Every subclass must specify the subset of ``LifecycleState`` that applies to it;
    validation runs through :class:`musubi.types.lifecycle_event.AllowedTransitions`.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        ser_json_timedelta="iso8601",
    )

    object_id: KSUID = Field(default_factory=generate_ksuid)
    namespace: Namespace
    # `identity_family` is the load-bearing field for cross-substrate
    # federation: aoi/command-chair, aoi/voice, aoi/shared all carry
    # identity_family="aoi", which lets retrieval, ranking, and synthesis
    # treat them as one continuous Aoi memory stream regardless of which
    # substrate captured a given memory. See `family_of` in musubi.types.common.
    #
    # Contract:
    # - The type is `str | None` only to express "not yet derived" at
    #   construction time. Persisted points always carry a non-empty
    #   string after the validator runs OR after the operator backfill
    #   has been applied. Downstream readers may assume a string in
    #   production but should guard for None when reading raw,
    #   unvalidated payloads (e.g. during partial migrations).
    # - The validator derives this from `namespace` only when the caller
    #   passes the auto-sentinel `None`. Explicit values (including
    #   "legacy-aoi" for migration scenarios) are preserved as-is.
    # - This validator runs again on assignment (`validate_assignment=True`),
    #   but because the auto-derive only kicks in for the sentinel, a
    #   later `obj.namespace = "yua/..."` will NOT silently re-derive
    #   `identity_family`. If a migration genuinely needs both to move
    #   together, the caller assigns both, or assigns `identity_family =
    #   None` to re-trigger derivation. This is intentional: it prevents
    #   namespace renames from silently re-homing data across identities.
    identity_family: str | None = None
    schema_version: int = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=utc_now)
    created_epoch: float | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    updated_epoch: float | None = None
    version: int = 1
    state: LifecycleState

    @model_validator(mode="after")
    def _fill_epochs_and_enforce_monotonicity(self) -> MusubiObject:
        # Always bypass validate_assignment when touching our own fields inside
        # a validator — direct attr set would re-enter the validator chain.
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))

        if self.created_epoch is None:
            object.__setattr__(self, "created_epoch", epoch_of(self.created_at))
        if self.updated_epoch is None:
            object.__setattr__(self, "updated_epoch", epoch_of(self.updated_at))

        if self.updated_epoch < self.created_epoch:  # type: ignore[operator]
            raise ValueError(
                f"updated_epoch ({self.updated_epoch}) < created_epoch "
                f"({self.created_epoch}) — timestamps must be monotone"
            )
        if self.version < 1:
            raise ValueError(f"version must start at 1, got {self.version}")

        # Auto-derive `identity_family` from `namespace` ONLY when the
        # caller passed the None sentinel. Explicit values are preserved
        # so migrations can decouple identity from namespace temporarily
        # (see the contract block on the field declaration above for
        # why we don't auto-update on later namespace mutations).
        if self.identity_family is None:
            object.__setattr__(self, "identity_family", family_of(self.namespace))

        return self


class MemoryObject(MusubiObject):
    """Objects that carry content + lineage.

    Concrete types (EpisodicMemory, CuratedKnowledge, SynthesizedConcept) override
    which fields are required and add type-specific ones.
    """

    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=10, default=5)
    reinforcement_count: int = Field(ge=0, default=0)
    last_accessed_at: datetime | None = None
    access_count: int = Field(ge=0, default=0)

    # Lineage
    supersedes: list[KSUID] = Field(default_factory=list)
    superseded_by: KSUID | None = None
    merged_from: list[KSUID] = Field(default_factory=list)
    linked_to_topics: list[str] = Field(default_factory=list)
    supported_by: list[ArtifactRef] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)
    derived_from: KSUID | None = None

    # Bitemporal validity (see [[04-data-model/temporal-model]])
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    valid_from_epoch: float | None = None
    valid_until_epoch: float | None = None

    @model_validator(mode="after")
    def _fill_validity_epochs_and_check(self) -> MemoryObject:
        if self.valid_from is not None:
            object.__setattr__(self, "valid_from", ensure_utc(self.valid_from))
            if self.valid_from_epoch is None:
                object.__setattr__(self, "valid_from_epoch", epoch_of(self.valid_from))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", ensure_utc(self.valid_until))
            if self.valid_until_epoch is None:
                object.__setattr__(self, "valid_until_epoch", epoch_of(self.valid_until))
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until < self.valid_from
        ):
            raise ValueError(f"valid_until ({self.valid_until}) < valid_from ({self.valid_from})")

        if self.last_accessed_at is not None:
            object.__setattr__(self, "last_accessed_at", ensure_utc(self.last_accessed_at))

        if self.superseded_by is not None and self.superseded_by == self.object_id:
            raise ValueError("an object cannot supersede itself")
        if self.object_id in self.supersedes:
            raise ValueError("an object cannot appear in its own supersedes list")

        return self


__all__ = ["MemoryObject", "MusubiObject"]
