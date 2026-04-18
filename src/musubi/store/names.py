"""Canonical collection names.

Names are frozen at v1; a rename would require a dual-write migration. Keep
them here so the rest of the code base never stringifies a collection name
inline.
"""

from __future__ import annotations

from typing import Final, Literal

CollectionName = Literal[
    "musubi_episodic",
    "musubi_curated",
    "musubi_concept",
    "musubi_artifact",
    "musubi_artifact_chunks",
    "musubi_thought",
    "musubi_lifecycle_events",
]

COLLECTION_NAMES: Final[tuple[CollectionName, ...]] = (
    "musubi_episodic",
    "musubi_curated",
    "musubi_concept",
    "musubi_artifact",
    "musubi_artifact_chunks",
    "musubi_thought",
    "musubi_lifecycle_events",
)

_PLANE_TO_COLLECTION: Final[dict[str, CollectionName]] = {
    "episodic": "musubi_episodic",
    "curated": "musubi_curated",
    "concept": "musubi_concept",
    "artifact": "musubi_artifact",
    "thought": "musubi_thought",
    "lifecycle": "musubi_lifecycle_events",
}


def collection_for_plane(plane: str) -> CollectionName:
    """Return the primary (single-point-per-object) collection for a plane.

    ``artifact`` maps to ``musubi_artifact`` (the metadata collection); chunk
    storage lives in the separate ``musubi_artifact_chunks`` collection.
    """
    try:
        return _PLANE_TO_COLLECTION[plane]
    except KeyError as exc:
        raise ValueError(
            f"unknown plane {plane!r}; expected one of {sorted(_PLANE_TO_COLLECTION.keys())}"
        ) from exc


__all__ = ["COLLECTION_NAMES", "CollectionName", "collection_for_plane"]
