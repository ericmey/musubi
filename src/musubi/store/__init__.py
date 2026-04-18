"""Qdrant storage layout: collection + index bootstrap.

See [[04-data-model/qdrant-layout]] for the authoritative spec. This package
owns the *structural* side of Qdrant — collection names, named-vector configs,
payload indexes, and the ``bootstrap()`` glue that brings a fresh or existing
Qdrant node into alignment with the spec.

It does **not** own: queries (see ``musubi.retrieve``), writes (see
``musubi.planes``), or embedding generation (see ``musubi.embedding``).
"""

from musubi.store.bootstrap import BootstrapReport, bootstrap
from musubi.store.collections import ensure_collections
from musubi.store.indexes import ensure_indexes
from musubi.store.names import COLLECTION_NAMES, CollectionName, collection_for_plane
from musubi.store.specs import (
    DENSE_VECTOR_NAME,
    INDEXES_BY_COLLECTION,
    REGISTRY,
    SPARSE_VECTOR_NAME,
    UNIVERSAL_INDEXES,
    CollectionSpec,
    IndexSpec,
    PayloadSchema,
)

__all__ = [
    "COLLECTION_NAMES",
    "DENSE_VECTOR_NAME",
    "INDEXES_BY_COLLECTION",
    "REGISTRY",
    "SPARSE_VECTOR_NAME",
    "UNIVERSAL_INDEXES",
    "BootstrapReport",
    "CollectionName",
    "CollectionSpec",
    "IndexSpec",
    "PayloadSchema",
    "bootstrap",
    "collection_for_plane",
    "ensure_collections",
    "ensure_indexes",
]
