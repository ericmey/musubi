"""End-to-end: ``bootstrap()`` should be idempotent and correctly report changes."""

from __future__ import annotations

import dataclasses

import pytest
from qdrant_client import QdrantClient

from musubi.store import COLLECTION_NAMES, bootstrap


class TestBootstrap:
    def test_first_boot_creates_all_collections(self, qdrant: QdrantClient) -> None:
        report = bootstrap(qdrant)
        assert set(report.collections_created) == set(COLLECTION_NAMES)
        assert report.any_changes is True

    def test_collections_exist_after_bootstrap(self, qdrant: QdrantClient) -> None:
        bootstrap(qdrant)
        present = {c.name for c in qdrant.get_collections().collections}
        assert present == set(COLLECTION_NAMES)

    def test_second_boot_does_not_recreate_collections(self, qdrant: QdrantClient) -> None:
        bootstrap(qdrant)
        second = bootstrap(qdrant)
        assert second.collections_created == []

    def test_report_dict_covers_every_collection(self, qdrant: QdrantClient) -> None:
        report = bootstrap(qdrant)
        assert set(report.indexes_created.keys()) == set(COLLECTION_NAMES)


class TestReportImmutability:
    def test_report_is_frozen(self, qdrant: QdrantClient) -> None:
        report = bootstrap(qdrant)
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.collections_created = []  # type: ignore[misc]
