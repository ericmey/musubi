"""Collection bootstrap — idempotent ``ensure_collections``.

Reads :data:`musubi.store.specs.REGISTRY` and creates every collection that
doesn't already exist on the target Qdrant node. Existing collections are
left alone — we never drop, rename, or rewrite vector config at boot. Vector
schema changes go through a separate (future) migration tool.
"""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from musubi.store.names import CollectionName
from musubi.store.specs import (
    DENSE_VECTOR_NAME,
    REGISTRY,
    SPARSE_VECTOR_NAME,
    CollectionSpec,
)


def build_vectors_config(spec: CollectionSpec) -> dict[str, models.VectorParams]:
    """Translate a :class:`CollectionSpec` to qdrant-client ``VectorParams``."""
    quantization: models.ScalarQuantization | None = None
    if spec.quantize_int8:
        quantization = models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8,
                quantile=0.99,
                always_ram=True,
            )
        )

    return {
        DENSE_VECTOR_NAME: models.VectorParams(
            size=spec.dense_size,
            distance=models.Distance.COSINE,
            on_disk=spec.on_disk_vectors,
            hnsw_config=models.HnswConfigDiff(
                m=spec.hnsw_m,
                ef_construct=spec.hnsw_ef_construct,
            ),
            quantization_config=quantization,
        )
    }


def build_sparse_vectors_config(
    spec: CollectionSpec,
) -> dict[str, models.SparseVectorParams] | None:
    """Translate a :class:`CollectionSpec` to qdrant-client ``SparseVectorParams``."""
    if not spec.has_sparse:
        return None
    return {
        SPARSE_VECTOR_NAME: models.SparseVectorParams(
            index=models.SparseIndexParams(
                on_disk=False,
                full_scan_threshold=spec.sparse_full_scan_threshold,
            )
        )
    }


def _existing_collection_names(client: QdrantClient) -> set[str]:
    return {c.name for c in client.get_collections().collections}


def ensure_collections(client: QdrantClient) -> list[CollectionName]:
    """Create any collection in :data:`REGISTRY` that's missing on ``client``.

    Returns the names of collections this call actually created. Safe to
    call repeatedly: existing collections are never touched.
    """
    existing = _existing_collection_names(client)
    created: list[CollectionName] = []
    for spec in REGISTRY:
        if spec.name in existing:
            continue
        client.create_collection(
            collection_name=spec.name,
            vectors_config=build_vectors_config(spec),
            sparse_vectors_config=build_sparse_vectors_config(spec),
        )
        created.append(spec.name)
    return created


__all__ = [
    "build_sparse_vectors_config",
    "build_vectors_config",
    "ensure_collections",
]
