"""``bootstrap()`` — glue that runs collection + index bring-up in order.

One entrypoint the Musubi process calls at startup. Safe to call on every
boot: idempotent by construction. Returns a :class:`BootstrapReport` so the
caller can log exactly what changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qdrant_client import QdrantClient

from musubi.store.collections import ensure_collections
from musubi.store.indexes import ensure_indexes
from musubi.store.names import CollectionName


@dataclass(frozen=True)
class BootstrapReport:
    """Summary of changes made by one :func:`bootstrap` call."""

    collections_created: list[CollectionName] = field(default_factory=list)
    indexes_created: dict[CollectionName, list[str]] = field(default_factory=dict)

    @property
    def any_changes(self) -> bool:
        return bool(self.collections_created) or any(v for v in self.indexes_created.values())


def bootstrap(client: QdrantClient) -> BootstrapReport:
    """Bring a Qdrant node into alignment with the declared store layout.

    Order matters: collections first, then indexes — an index can't be
    created on a collection that doesn't exist yet. Both steps are
    idempotent, so the whole call is safe to repeat.
    """
    collections_created = ensure_collections(client)
    indexes_created = ensure_indexes(client)
    return BootstrapReport(
        collections_created=collections_created,
        indexes_created=indexes_created,
    )


__all__ = ["BootstrapReport", "bootstrap"]
