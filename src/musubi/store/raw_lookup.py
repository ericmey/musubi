"""Raw, un-deserialized point lookups — the door that still opens on a broken row.

Every plane's ``get()`` ends in ``Model.model_validate(payload)``, and every canonical
model is ``extra="forbid"``. That is the right contract for *reading* a memory: the
model is what makes a memory mean something.

It is the wrong contract for *asking about* one.

Until 2026-07-11 every delete and archive path in Musubi guarded existence with
``plane.get()``. So a row carrying an unmodeled payload key raised **inside the
404-guard**, before the delete ran — and the row became **unremovable precisely
because it was broken**. That is exactly backwards, and for a memory substrate it is
the worst possible way to be wrong: a false or corrupted memory that cannot be deleted
is one that keeps teaching a falsehood to every agent that reads the plane, forever.
On the curated plane — shared settled truth, read as fact by every agent — it would be
permanent false ground.

So: **the removability of a memory must never depend on that memory being valid.**

These helpers answer the two questions that must keep working when the model refuses:

- ``point_exists()`` — is it there? (``with_payload=False``: nothing to validate)
- ``raw_payload()``  — what is actually stored? (unvalidated; the repair/inspection door)

Both take the client and collection directly rather than a plane, because the planes
share no base class. Each plane exposes them as ``exists()`` / ``raw_payload()`` and
delegates here, so the semantics live in one place instead of drifting across five.

Callers of ``raw_payload()`` must treat every key as untrusted — ``.get()`` with a
default, never index. A corrupted row may be missing or malforming anything.

Found across planes by Yua in adversarial review of PR #398, after the episodic-only
fix was proposed as complete. It was not.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models


def _by_id(namespace: str, object_id: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
        ]
    )


def point_exists(client: QdrantClient, collection: str, *, namespace: str, object_id: str) -> bool:
    """Is this object present? Answered from the index, never from the model.

    NOTE the reachability limit shared by every payload-filtered lookup here: this finds
    the row by its ``namespace`` and ``object_id`` **payload fields**. A row whose payload
    is missing or malforming *those* keys is invisible to it. For deletion — where being
    unreachable is fatal — use :func:`retrieve_by_point_id`, which addresses the point
    directly and does not care what the payload says.
    """
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=_by_id(namespace, object_id),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return bool(records)


def raw_payload(
    client: QdrantClient, collection: str, *, namespace: str, object_id: str
) -> dict[str, Any] | None:
    """The payload exactly as persisted — never model-validated.

    ``None`` means **the point does not exist**. An existing point whose payload is empty
    returns ``{}`` — an empty dict, not ``None``.

    That distinction is load-bearing and was wrong in the first cut: returning ``None`` for
    both conflated "absent" with "present but corrupt", so ``EpisodicPlane.delete()`` raised
    ``LookupError`` on an empty-payload row and refused to remove it. That is the same class
    of bug this whole module exists to kill — a corruption shape that makes a memory
    *undeletable because it is broken*. Callers must test ``is None``, never truthiness.
    (Yua, rev2 review of PR #398.)

    Subject to the same payload-filter reachability limit as :func:`point_exists`.
    """
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=_by_id(namespace, object_id),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not records:
        return None
    # `or {}` — NOT `or None`. An existing point with no payload still EXISTS.
    return records[0].payload or {}


def retrieve_by_point_id(
    client: QdrantClient, collection: str, *, point_id: str
) -> dict[str, Any] | None:
    """Fetch a payload by its Qdrant point ID, bypassing payload filters entirely.

    This is the lookup of last resort, and the only one that still works when a row's
    payload has lost the very identifiers everything else searches by. Every plane derives
    its point ID deterministically (``uuid5(_POINT_NS, object_id)``), so a caller holding
    an ``object_id`` can always address the point **even if that object_id no longer
    appears in the payload**.

    ``None`` means the point does not exist. ``{}`` means it exists with an empty payload.

    Deletion must go through this. A memory that cannot be addressed cannot be removed, and
    a memory that cannot be removed can keep teaching a falsehood forever.
    """
    points = client.retrieve(
        collection_name=collection,
        ids=[point_id],
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    return dict(points[0].payload or {})


__all__ = ["point_exists", "raw_payload", "retrieve_by_point_id"]
