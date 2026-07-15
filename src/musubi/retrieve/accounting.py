"""RET-002 / #500 + RET-008 / #502 — final-delivery access accounting, concurrency-safe.

The single seam that records "this stored row was actually delivered to a caller." It runs
ONCE, at the final retrieval boundary (``orchestration.retrieve``, immediately before
``_finalize``), over exactly the delivered rows — after fanout, dedup, sorting, and limit.
Never a dropped candidate, and independent of lineage hydration (which no longer accounts;
see the ``bump_access=False`` sites in ``deep._hydrate_one``).

Accountable planes are those whose type carries ``access_count`` — episodic, curated, concept
(all extend ``MemoryObject``). artifact and thought extend ``MusubiObject`` and intentionally
lack the field, so their delivered rows are a deliberate, tested no-op.

**Concurrency (RET-008 / #502).** The increment goes through the shared fenced per-record lease
(:func:`musubi.store.access_lease.lease_increment_access`), so concurrent deliveries lose no
increment under real parallelism (multiple workers/processes, a future async client, or a
concurrent cross-process writer). Lease exhaustion is FAIL-LOUD: it raises, and
``orchestration.retrieve`` normalizes it to a typed ``Err`` (and ``/v1/context`` to an INTERNAL
APIError). Within a single event loop the synchronous Qdrant client blocks the loop across a whole
read→write, so same-loop deliveries already serialize (guarded by a unit test); the lease is for
real cross-thread/-process/-worker parallelism.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient

from musubi.store.access_lease import lease_increment_access
from musubi.store.names import collection_for_plane

#: Planes whose type (``MemoryObject``) carries an ``access_count`` field.
ACCOUNTABLE_PLANES = frozenset({"episodic", "curated", "concept"})


async def account_delivered(client: QdrantClient, results: list[Any]) -> None:
    """Account each delivered row exactly once, concurrency-safe and batched per collection.

    ``results`` is the finalized delivered list — already deduped/sorted/limited. Rows on
    non-accountable planes (artifact/thought) are skipped. Does not mutate ``results``. Raises
    :class:`~musubi.store.access_lease.AccessLeaseExhausted` if lease contention cannot be resolved
    within the retry budget (fail-loud — the caller finalizes it as a typed error).
    """
    by_plane: dict[str, set[tuple[str, str]]] = {}
    for row in results:
        plane = getattr(row, "plane", None)
        object_id = getattr(row, "object_id", None)
        namespace = getattr(row, "namespace", None)
        if plane in ACCOUNTABLE_PLANES and object_id and namespace:
            by_plane.setdefault(plane, set()).add((namespace, object_id))

    for plane, pairs in by_plane.items():
        await lease_increment_access(client, collection_for_plane(plane), pairs)


__all__ = ["ACCOUNTABLE_PLANES", "account_delivered"]
