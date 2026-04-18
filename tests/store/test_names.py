"""Tests for ``musubi.store.names``."""

from __future__ import annotations

import pytest

from musubi.store import COLLECTION_NAMES, collection_for_plane


def test_all_seven_collections_listed() -> None:
    assert set(COLLECTION_NAMES) == {
        "musubi_episodic",
        "musubi_curated",
        "musubi_concept",
        "musubi_artifact",
        "musubi_artifact_chunks",
        "musubi_thought",
        "musubi_lifecycle_events",
    }


def test_collection_names_start_with_musubi_prefix() -> None:
    for name in COLLECTION_NAMES:
        assert name.startswith("musubi_")


@pytest.mark.parametrize(
    ("plane", "expected"),
    [
        ("episodic", "musubi_episodic"),
        ("curated", "musubi_curated"),
        ("concept", "musubi_concept"),
        ("artifact", "musubi_artifact"),
        ("thought", "musubi_thought"),
        ("lifecycle", "musubi_lifecycle_events"),
    ],
)
def test_plane_to_collection_mapping(plane: str, expected: str) -> None:
    assert collection_for_plane(plane) == expected


def test_unknown_plane_rejected() -> None:
    with pytest.raises(ValueError, match="unknown plane"):
        collection_for_plane("nonsense")
