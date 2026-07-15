"""RET-008 / #502 — one shared serialization boundary for MemoryObject payload writes.

``access_count``, ``last_accessed_at`` and ``access_lease_token`` are owned by the fenced access
lease (:mod:`musubi.store.access_lease`). Any full-payload UPDATE that dumps an in-memory model
and writes it back (state transitions, supersession, reinforcement, dedup-merge) would carry a
STALE copy of these fields and silently overwrite a concurrent leased increment. So:

- **CREATE** writes them explicitly (``access_count = 0`` — lifecycle demotion keys on a fresh
  row having ``access_count == 0``).
- **UPDATE** must NOT write them: use :func:`memory_update_payload` (a ``set_payload`` merge, which
  only overwrites the keys it is given, leaving the lease-owned fields untouched). A legacy
  full-point ``upsert`` can use :func:`preserve_lease_fields` to refresh the STORED values and
  narrow the stale window, but that refresh and the upsert are not atomic; DATA-001 / #530 owns
  removal of that residual cross-mutation race.

A full-payload write that bypasses this boundary invalidates the lease mechanism.
"""

from __future__ import annotations

from typing import Any, cast

#: Fields owned by the access lease — never written by a full-payload UPDATE.
LEASE_OWNED_FIELDS = frozenset({"access_count", "last_accessed_at", "access_lease_token"})


def memory_update_payload(model: Any) -> dict[str, Any]:
    """``model_dump(mode="json")`` for an UPDATE/transition write, EXCLUDING the lease-owned fields.

    Intended for ``set_payload`` (a merge): the stored ``access_count`` / ``last_accessed_at`` /
    ``access_lease_token`` are left exactly as the lease last wrote them, so a concurrent leased
    increment is never reset.
    """
    return cast("dict[str, Any]", model.model_dump(mode="json", exclude=set(LEASE_OWNED_FIELDS)))


def preserve_lease_fields(payload: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """For a full-point ``upsert`` UPDATE that cannot merge: carry the STORED lease-owned values
    into ``payload`` (dropping the in-memory model's earlier stale copies). This narrows but cannot
    close the refresh-to-upsert race because the two operations are not atomic; DATA-001 / #530
    owns that remaining correction. ``stored`` is the row's current Qdrant payload (``None`` →
    treat as absent)."""
    out = {k: v for k, v in payload.items() if k not in LEASE_OWNED_FIELDS}
    for field in LEASE_OWNED_FIELDS:
        if stored and field in stored:
            out[field] = stored[field]
    return out


__all__ = ["LEASE_OWNED_FIELDS", "memory_update_payload", "preserve_lease_fields"]
