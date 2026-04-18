"""Structural specs for every Qdrant collection we run.

Two immutable registries:

- ``REGISTRY`` — one :class:`CollectionSpec` per collection.
- ``INDEXES_BY_COLLECTION`` — one :class:`IndexSpec` list per collection
  (the *delta* only; universal indexes live in ``UNIVERSAL_INDEXES`` and are
  applied to every collection automatically by :func:`ensure_indexes`).

If you're adding a new collection or a new payload index, touch this file
and this file only. The bootstrap functions read from these registries — no
other module should duplicate collection- or index-metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from musubi.store.names import CollectionName

DENSE_VECTOR_NAME: Final[str] = "dense_bge_m3_v1"
SPARSE_VECTOR_NAME: Final[str] = "sparse_splade_v1"
DENSE_SIZE: Final[int] = 1024

PayloadSchema = Literal["keyword", "integer", "float", "bool", "text", "datetime"]


@dataclass(frozen=True)
class CollectionSpec:
    """Declarative shape of a single Qdrant collection.

    Translated to ``qdrant_client.models.VectorParams`` /
    ``SparseVectorParams`` / quantization config by :func:`ensure_collections`.
    """

    name: CollectionName
    has_sparse: bool = True
    dense_size: int = DENSE_SIZE
    hnsw_m: int = 32
    hnsw_ef_construct: int = 256
    quantize_int8: bool = True
    sparse_full_scan_threshold: int = 5000
    on_disk_vectors: bool = False


@dataclass(frozen=True)
class IndexSpec:
    """Declarative shape of a single payload index."""

    field_name: str
    schema: PayloadSchema


# --------------------------------------------------------------------------
# Registry — one CollectionSpec per collection listed in [[04-data-model/qdrant-layout#Collections]]
# --------------------------------------------------------------------------

REGISTRY: Final[tuple[CollectionSpec, ...]] = (
    CollectionSpec(name="musubi_episodic"),
    CollectionSpec(name="musubi_curated"),
    CollectionSpec(name="musubi_concept"),
    CollectionSpec(name="musubi_artifact_chunks"),
    # Metadata-only collection: dense (title+summary) but no sparse.
    CollectionSpec(name="musubi_artifact", has_sparse=False),
    # Thoughts: dense optional-but-enabled today; drop under load (tracked in spec).
    CollectionSpec(name="musubi_thought"),
    # Audit-log mirror: dense on "reason" for semantic search over the log.
    CollectionSpec(name="musubi_lifecycle_events", has_sparse=False),
)


# --------------------------------------------------------------------------
# Universal payload indexes — applied to every collection
# --------------------------------------------------------------------------

UNIVERSAL_INDEXES: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="namespace", schema="keyword"),
    IndexSpec(field_name="object_id", schema="keyword"),
    IndexSpec(field_name="state", schema="keyword"),
    IndexSpec(field_name="schema_version", schema="integer"),
    IndexSpec(field_name="tags", schema="keyword"),  # array-valued
    IndexSpec(field_name="topics", schema="keyword"),
    IndexSpec(field_name="created_epoch", schema="float"),
    IndexSpec(field_name="updated_epoch", schema="float"),
    IndexSpec(field_name="importance", schema="integer"),
    IndexSpec(field_name="version", schema="integer"),
)


# --------------------------------------------------------------------------
# Per-collection delta indexes
# --------------------------------------------------------------------------

_EPISODIC_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="content_type", schema="keyword"),
    IndexSpec(field_name="capture_source", schema="keyword"),
    IndexSpec(field_name="capture_presence", schema="keyword"),
    IndexSpec(field_name="access_count", schema="integer"),
    IndexSpec(field_name="reinforcement_count", schema="integer"),
    IndexSpec(field_name="last_accessed_epoch", schema="float"),
    IndexSpec(field_name="supported_by.artifact_id", schema="keyword"),
    IndexSpec(field_name="merged_into", schema="keyword"),
    IndexSpec(field_name="superseded_by", schema="keyword"),
)

_CURATED_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="vault_path", schema="keyword"),
    IndexSpec(field_name="musubi_managed", schema="bool"),
    IndexSpec(field_name="valid_from_epoch", schema="float"),
    IndexSpec(field_name="valid_until_epoch", schema="float"),
    IndexSpec(field_name="promoted_from", schema="keyword"),
    IndexSpec(field_name="supersedes", schema="keyword"),
    IndexSpec(field_name="superseded_by", schema="keyword"),
    IndexSpec(field_name="body_hash", schema="keyword"),
    IndexSpec(field_name="read_by", schema="keyword"),
)

_CONCEPT_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="promoted_to", schema="keyword"),
    IndexSpec(field_name="promotion_attempts", schema="integer"),
    IndexSpec(field_name="merged_from", schema="keyword"),
    IndexSpec(field_name="merged_from_planes", schema="keyword"),
    IndexSpec(field_name="contradicts", schema="keyword"),
    IndexSpec(field_name="last_reinforced_epoch", schema="float"),
)

_ARTIFACT_CHUNK_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="artifact_id", schema="keyword"),
    IndexSpec(field_name="chunk_id", schema="keyword"),
    IndexSpec(field_name="chunk_index", schema="integer"),
    IndexSpec(field_name="content_type", schema="keyword"),
    IndexSpec(field_name="chunker", schema="keyword"),
    IndexSpec(field_name="source_system", schema="keyword"),
)

_ARTIFACT_METADATA_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="sha256", schema="keyword"),
    IndexSpec(field_name="source_system", schema="keyword"),
    IndexSpec(field_name="source_ref", schema="keyword"),
    IndexSpec(field_name="ingested_by", schema="keyword"),
    IndexSpec(field_name="artifact_state", schema="keyword"),
    IndexSpec(field_name="derived_from", schema="keyword"),
)

_THOUGHT_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="from_presence", schema="keyword"),
    IndexSpec(field_name="to_presence", schema="keyword"),
    IndexSpec(field_name="channel", schema="keyword"),
    IndexSpec(field_name="read", schema="bool"),
    IndexSpec(field_name="read_by", schema="keyword"),
    IndexSpec(field_name="in_reply_to", schema="keyword"),
)

_LIFECYCLE_EVENT_DELTAS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(field_name="event_id", schema="keyword"),
    IndexSpec(field_name="object_type", schema="keyword"),
    IndexSpec(field_name="from_state", schema="keyword"),
    IndexSpec(field_name="to_state", schema="keyword"),
    IndexSpec(field_name="actor", schema="keyword"),
    IndexSpec(field_name="occurred_epoch", schema="float"),
    IndexSpec(field_name="correlation_id", schema="keyword"),
)


INDEXES_BY_COLLECTION: Final[dict[CollectionName, tuple[IndexSpec, ...]]] = {
    "musubi_episodic": _EPISODIC_DELTAS,
    "musubi_curated": _CURATED_DELTAS,
    "musubi_concept": _CONCEPT_DELTAS,
    "musubi_artifact_chunks": _ARTIFACT_CHUNK_DELTAS,
    "musubi_artifact": _ARTIFACT_METADATA_DELTAS,
    "musubi_thought": _THOUGHT_DELTAS,
    "musubi_lifecycle_events": _LIFECYCLE_EVENT_DELTAS,
}


def all_indexes_for(name: CollectionName) -> tuple[IndexSpec, ...]:
    """Return the full (universal + per-collection) index list for a collection."""
    return UNIVERSAL_INDEXES + INDEXES_BY_COLLECTION[name]


__all__ = [
    "DENSE_SIZE",
    "DENSE_VECTOR_NAME",
    "INDEXES_BY_COLLECTION",
    "REGISTRY",
    "SPARSE_VECTOR_NAME",
    "UNIVERSAL_INDEXES",
    "CollectionSpec",
    "IndexSpec",
    "PayloadSchema",
    "all_indexes_for",
]
