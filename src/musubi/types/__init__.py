"""Shared pydantic types.

Every public payload crossing a module boundary is a pydantic model defined
here. See slice-types in the vault's ``_slices/`` registry and the specs in
``04-data-model/`` for design.

Public surface:

- Primitives: ``LifecycleState``, ``Modality``, ``ArtifactIndexingState``,
  ``Namespace``, ``KSUID``, ``Result``, ``Ok``, ``Err``, ``ArtifactRef``,
  ``SCHEMA_VERSION``.
- Base: ``MusubiObject``, ``MemoryObject``.
- Concrete: ``EpisodicMemory``, ``CuratedKnowledge``, ``SynthesizedConcept``,
  ``Thought``, ``SourceArtifact``, ``ArtifactChunk``.
- Lifecycle: ``LifecycleEvent``, ``is_legal_transition``,
  ``legal_next_states``, ``allowed_states``, ``ObjectType``.
- Helpers: ``utc_now``, ``epoch_of``, ``ensure_utc``, ``generate_ksuid``,
  ``validate_namespace``, ``validate_ksuid``.
"""

from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.base import MemoryObject, MusubiObject
from musubi.types.common import (
    KSUID,
    NAMESPACE_RE,
    SCHEMA_VERSION,
    ArtifactIndexingState,
    ArtifactRef,
    Err,
    LifecycleState,
    Modality,
    Namespace,
    Ok,
    Result,
    ensure_utc,
    epoch_of,
    generate_ksuid,
    utc_now,
    validate_ksuid,
    validate_namespace,
)
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory
from musubi.types.lifecycle_event import (
    LifecycleEvent,
    ObjectType,
    allowed_states,
    is_legal_transition,
    legal_next_states,
)
from musubi.types.thought import Thought

__all__ = [
    "KSUID",
    "NAMESPACE_RE",
    "SCHEMA_VERSION",
    "ArtifactChunk",
    "ArtifactIndexingState",
    "ArtifactRef",
    "CuratedKnowledge",
    "EpisodicMemory",
    "Err",
    "LifecycleEvent",
    "LifecycleState",
    "MemoryObject",
    "Modality",
    "MusubiObject",
    "Namespace",
    "ObjectType",
    "Ok",
    "Result",
    "SourceArtifact",
    "SynthesizedConcept",
    "Thought",
    "allowed_states",
    "ensure_utc",
    "epoch_of",
    "generate_ksuid",
    "is_legal_transition",
    "legal_next_states",
    "utc_now",
    "validate_ksuid",
    "validate_namespace",
]
