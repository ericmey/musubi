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
    """Is this object present? Answered from the index, never from the model."""
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

    Returns ``None`` when the point does not exist. An existing point with an empty
    payload also returns ``None``; callers asking a pure existence question should use
    :func:`point_exists`, which does not conflate the two.
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
    return records[0].payload or None


__all__ = ["point_exists", "raw_payload"]
