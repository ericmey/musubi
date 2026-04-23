"""Tests for ``musubi.store.specs`` — the collection + index registries."""

from __future__ import annotations

from musubi.store import (
    COLLECTION_NAMES,
    INDEXES_BY_COLLECTION,
    REGISTRY,
    UNIVERSAL_INDEXES,
)
from musubi.store.specs import all_indexes_for, collection_has_sparse


class TestRegistry:
    def test_registry_covers_every_collection_name(self) -> None:
        assert {spec.name for spec in REGISTRY} == set(COLLECTION_NAMES)

    def test_all_dense_1024d(self) -> None:
        assert all(spec.dense_size == 1024 for spec in REGISTRY)

    def test_sparse_opt_in_per_collection(self) -> None:
        by_name = {spec.name: spec for spec in REGISTRY}
        assert by_name["musubi_artifact"].has_sparse is False
        assert by_name["musubi_lifecycle_events"].has_sparse is False
        assert by_name["musubi_episodic"].has_sparse is True
        assert by_name["musubi_curated"].has_sparse is True
        assert by_name["musubi_concept"].has_sparse is True
        assert by_name["musubi_artifact_chunks"].has_sparse is True
        assert by_name["musubi_thought"].has_sparse is True

    def test_hnsw_m_tuned_up(self) -> None:
        # Per [[04-data-model/qdrant-layout#Named vectors]], we raise m slightly
        # above the qdrant default (16) for recall.
        assert all(spec.hnsw_m >= 32 for spec in REGISTRY)

    def test_int8_quantization_on_by_default(self) -> None:
        assert all(spec.quantize_int8 for spec in REGISTRY)


class TestIndexRegistry:
    def test_every_collection_has_a_delta_entry(self) -> None:
        assert set(INDEXES_BY_COLLECTION.keys()) == set(COLLECTION_NAMES)

    def test_universal_indexes_present_for_every_collection(self) -> None:
        universal_fields = {spec.field_name for spec in UNIVERSAL_INDEXES}
        for name in COLLECTION_NAMES:
            full = {spec.field_name for spec in all_indexes_for(name)}
            assert universal_fields.issubset(full), (
                f"{name} missing universal indexes: {universal_fields - full}"
            )

    def test_namespace_universally_indexed(self) -> None:
        # Namespace is the load-bearing isolation filter; drop it and every
        # query potentially leaks across tenants.
        assert "namespace" in {spec.field_name for spec in UNIVERSAL_INDEXES}

    def test_body_hash_present_on_curated(self) -> None:
        # Echo detection on vault-sync depends on this index.
        fields = {spec.field_name for spec in INDEXES_BY_COLLECTION["musubi_curated"]}
        assert "body_hash" in fields

    def test_episodic_supported_by_nested_path_indexed(self) -> None:
        fields = {spec.field_name for spec in INDEXES_BY_COLLECTION["musubi_episodic"]}
        assert "supported_by.artifact_id" in fields

    def test_bool_schema_used_correctly(self) -> None:
        bool_fields_curated = {
            spec.field_name
            for spec in INDEXES_BY_COLLECTION["musubi_curated"]
            if spec.schema == "bool"
        }
        assert bool_fields_curated == {"musubi_managed"}


class TestCollectionHasSparse:
    def test_dense_only_collections_return_false(self) -> None:
        assert collection_has_sparse("musubi_artifact") is False
        assert collection_has_sparse("musubi_lifecycle_events") is False

    def test_hybrid_collections_return_true(self) -> None:
        assert collection_has_sparse("musubi_episodic") is True
        assert collection_has_sparse("musubi_curated") is True
        assert collection_has_sparse("musubi_concept") is True
        assert collection_has_sparse("musubi_artifact_chunks") is True
        assert collection_has_sparse("musubi_thought") is True

    def test_unknown_collection_defaults_true(self) -> None:
        # Unknown names should fail loudly at the Qdrant boundary rather
        # than silently degrade to dense-only — see docstring on helper.
        assert collection_has_sparse("musubi_typo") is True


class TestImmutability:
    def test_registry_is_tuple(self) -> None:
        assert isinstance(REGISTRY, tuple)

    def test_universal_indexes_is_tuple(self) -> None:
        assert isinstance(UNIVERSAL_INDEXES, tuple)
