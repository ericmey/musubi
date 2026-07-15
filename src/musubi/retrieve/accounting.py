"""RET-002 / Issue #500 — final-delivery access accounting.

The single seam that records "this stored row was actually delivered to a caller." It runs
ONCE, at the final retrieval boundary (``orchestration.retrieve``, immediately before
``_finalize``), over exactly the delivered rows — after fanout, dedup, sorting, and limit.
Never a dropped candidate, and independent of lineage hydration (which no longer accounts;
see the ``bump_access=False`` sites in ``deep._hydrate_one``).

Accountable planes are those whose type carries ``access_count`` — episodic, curated, concept
(all extend ``MemoryObject``). artifact and thought extend ``MusubiObject`` and intentionally
lack the field, so their delivered rows are a deliberate, tested no-op. Giving them the field
is a schema change, out of RET-002 scope.

Concurrency: the increment is a batched read-modify-write (one scroll + one batch write per
accountable collection), NOT atomic — two concurrent retrievals of the same row can lose an
increment. This slice preserves the existing RMW semantics; true concurrent-counter safety is
tracked separately as Issue #502 and is deliberately NOT solved here.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models

from musubi.store.names import collection_for_plane
from musubi.types.common import utc_now

#: Planes whose type (``MemoryObject``) carries an ``access_count`` field.
ACCOUNTABLE_PLANES = frozenset({"episodic", "curated", "concept"})


async def account_delivered(client: QdrantClient, results: list[Any]) -> None:
    """Account each delivered row exactly once, batched per accountable collection.

    ``results`` is the finalized delivered list — already deduped/sorted/limited — so a single
    pass here is exactly-once by construction. Rows on non-accountable planes (artifact/thought)
    are skipped. Does not mutate ``results``.
    """
    by_plane: dict[str, set[tuple[str, str]]] = {}
    for row in results:
        plane = getattr(row, "plane", None)
        object_id = getattr(row, "object_id", None)
        namespace = getattr(row, "namespace", None)
        if plane in ACCOUNTABLE_PLANES and object_id and namespace:
            by_plane.setdefault(plane, set()).add((namespace, object_id))

    if not by_plane:
        return

    now_str = utc_now().isoformat().replace("+00:00", "Z")

    for plane, pairs in by_plane.items():
        collection = collection_for_plane(plane)
        # One batched READ, scoped to the EXACT delivered (namespace, object_id) pairs — the
        # codebase's tenant-scoping pattern (raw_lookup._by_id), not object_id alone. A point whose
        # stored namespace differs from a delivered row's is never touched. `should` = OR over the
        # per-pair `must` (namespace AND object_id); still ONE scroll + ONE write per collection.
        records, _ = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                should=[
                    models.Filter(
                        must=[
                            models.FieldCondition(
                                key="namespace", match=models.MatchValue(value=ns)
                            ),
                            models.FieldCondition(
                                key="object_id", match=models.MatchValue(value=oid)
                            ),
                        ]
                    )
                    for ns, oid in pairs
                ]
            ),
            limit=len(pairs),
            with_payload=True,
            with_vectors=False,
        )
        updates = [
            models.SetPayloadOperation(
                set_payload=models.SetPayload(
                    payload={
                        "access_count": (record.payload or {}).get("access_count", 0) + 1,
                        "last_accessed_at": now_str,
                    },
                    points=[record.id],
                )
            )
            for record in records
        ]
        # One batched WRITE per collection — never N+1.
        if updates:
            client.batch_update_points(collection_name=collection, update_operations=updates)


__all__ = ["ACCOUNTABLE_PLANES", "account_delivered"]
