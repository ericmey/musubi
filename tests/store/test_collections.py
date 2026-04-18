"""Tests for ``ensure_collections``.

Uses the in-memory qdrant-client (not a mock) so we get faithful vector/sparse
config roundtripping. Idempotency and quantization presence are both
verifiable locally.
"""

from __future__ import annotations

import warnings

from qdrant_client import QdrantClient

from musubi.store import COLLECTION_NAMES, ensure_collections
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME


class TestCreatesAllCollections:
    def test_first_boot_creates_every_collection(self, qdrant: QdrantClient) -> None:
        created = ensure_collections(qdrant)
        assert set(created) == set(COLLECTION_NAMES)

    def test_every_collection_now_exists(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        present = {c.name for c in qdrant.get_collections().collections}
        assert present == set(COLLECTION_NAMES)


class TestIdempotency:
    def test_second_boot_creates_nothing(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        second = ensure_collections(qdrant)
        assert second == []

    def test_third_boot_still_creates_nothing(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        ensure_collections(qdrant)
        third = ensure_collections(qdrant)
        assert third == []


class TestVectorConfig:
    def test_dense_vector_named_and_1024d(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        info = qdrant.get_collection("musubi_episodic")
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict)
        assert DENSE_VECTOR_NAME in vectors
        assert vectors[DENSE_VECTOR_NAME].size == 1024

    def test_quantization_applied_to_dense(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        info = qdrant.get_collection("musubi_episodic")
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict)
        quant = vectors[DENSE_VECTOR_NAME].quantization_config
        assert quant is not None, "INT8 scalar quantization must be configured"

    def test_sparse_present_on_sparse_collections(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        info = qdrant.get_collection("musubi_episodic")
        sparse = info.config.params.sparse_vectors
        assert sparse is not None
        assert SPARSE_VECTOR_NAME in sparse

    def test_sparse_absent_on_artifact_metadata(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        info = qdrant.get_collection("musubi_artifact")
        sparse = info.config.params.sparse_vectors
        # The metadata collection is title+summary dense-only per the spec.
        assert not sparse

    def test_hnsw_m_is_32(self, qdrant: QdrantClient) -> None:
        ensure_collections(qdrant)
        info = qdrant.get_collection("musubi_episodic")
        vectors = info.config.params.vectors
        assert isinstance(vectors, dict)
        hnsw = vectors[DENSE_VECTOR_NAME].hnsw_config
        assert hnsw is not None
        assert hnsw.m == 32


class TestPartialStateTolerance:
    def test_existing_subset_is_not_re_created(self, qdrant: QdrantClient) -> None:
        """If one collection already exists, the others are created; the pre-existing one is left alone."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # Manually create one of the collections with a minimal config so
            # we can detect "was not touched" by comparing vectors back.
            from qdrant_client import models

            qdrant.create_collection(
                "musubi_episodic",
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=1024, distance=models.Distance.COSINE
                    )
                },
            )

        created = ensure_collections(qdrant)
        assert "musubi_episodic" not in created
        assert set(created) == set(COLLECTION_NAMES) - {"musubi_episodic"}
